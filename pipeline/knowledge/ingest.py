"""Ingesta automática de series oficiales en ref.observations (fase D).

`uv run conocimiento ingerir` descarga cada indicador del catálogo con conector automático
(INE, Eurostat, Seguridad Social), calcula una huella de los datos y, si no ha cambiado
desde la última vez (ops.source_state), no toca la base. Si cambió, valida y carga con
`load_series` (lo nuevo aprobado, lo dudoso en cuarentena, las revisiones como versión
nueva) dentro de una ejecución registrada en ops.ingest_runs. Un fallo de un indicador se
registra y los demás siguen. Al final se avisa de cuarentenas, errores y datos que llevan
más tiempo sin actualizarse del que admite el catálogo (max_antiguedad_dias).
"""

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import httpx
import psycopg

from pipeline.indicator_update import (
    SINCE_YEAR,
    SPECS,
    EurostatSpec,
    IndicatorUpdateError,
    IneSpec,
    build_affiliation,
    build_eurostat,
    build_ine,
)
from pipeline.indicators import Indicator
from pipeline.knowledge.catalog import IndicatorSpec
from pipeline.knowledge.observations_load import LoadSummary, candidates_from_json, load_series

logger = logging.getLogger(__name__)

AUTOMATIC = ("ine", "eurostat", "seguridad_social")
INE_SERIES_URL = "https://www.ine.es/consul/serie.do?s={cod}"
# INE tables of the series already in the JSON base (human-readable links).
_INE_TABLES = {s.cod: s.tabla for s in SPECS if isinstance(s, IneSpec)}
_PERIODICITY = {"mensual": "mensual", "trimestral": "trimestral", "anual": "anual"}

Fetcher = Callable[[IndicatorSpec, httpx.Client, dt.date], Indicator]


class IngestError(RuntimeError):
    """An indicator cannot be ingested (catalog without a source code, bad answer…)."""


def _ine(
    spec: IndicatorSpec, client: httpx.Client, today: dt.date, since_year: int = SINCE_YEAR
) -> Indicator:
    if not spec.codigo_fuente:
        raise IngestError(f"{spec.id}: el catálogo no tiene codigo_fuente")
    periodicity = "puntual" if spec.frecuencia == "puntual" else _PERIODICITY[spec.frecuencia]
    ine = IneSpec(
        id=spec.id,
        nombre=spec.nombre,
        unidad=spec.unidad,
        cod=spec.codigo_fuente,
        tabla=_INE_TABLES.get(spec.codigo_fuente, 0),
        documento=spec.nombre,
        periodicidad=periodicity,  # type: ignore[arg-type]
    )
    ind = build_ine(ine, client, today=today, since_year=since_year)
    if not ine.tabla:  # no known table: link to the series itself
        url = INE_SERIES_URL.format(cod=spec.codigo_fuente)
        for o in ind.serie:
            o.fuente.url = url
    return ind


def _eurostat(
    spec: IndicatorSpec, client: httpx.Client, today: dt.date, since_year: int = SINCE_YEAR
) -> Indicator:
    if not spec.codigo_fuente or "?" not in spec.codigo_fuente:
        raise IngestError(f"{spec.id}: codigo_fuente de Eurostat debe ser dataset?filtro=valor&…")
    dataset, query = spec.codigo_fuente.split("?", 1)
    filters = dict(pair.split("=", 1) for pair in query.split("&"))
    return build_eurostat(
        EurostatSpec(id=spec.id, nombre=spec.nombre, unidad=spec.unidad, dataset=dataset,
                     filtros=filters),
        client,
        today=today,
        since_year=since_year,
    )  # fmt: skip


def _social_security(
    spec: IndicatorSpec, client: httpx.Client, today: dt.date, since_year: int = SINCE_YEAR
) -> Indicator:
    if spec.id != "afiliacion_ss" or not spec.codigo_fuente:
        raise IngestError(f"{spec.id}: no hay conector de la Seguridad Social para él")
    old, new = spec.codigo_fuente.split(",")
    ind = build_affiliation(client, today=today, queries=(old.strip(), new.strip()))
    return ind.model_copy(update={"id": spec.id})


FETCHERS: dict[str, Fetcher] = {
    "ine": _ine,
    "eurostat": _eurostat,
    "seguridad_social": _social_security,
}


def fingerprint(ind: Indicator) -> str:
    """Hash of the values only: a new download with the same numbers changes nothing."""
    rows = [(o.desde.isoformat(), o.hasta.isoformat(), round(o.valor, 9)) for o in ind.serie]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


@dataclass
class IndicatorResult:
    indicator_id: str
    status: str  # "sin_cambios" | "cargado" | "error"
    summary: LoadSummary | None = None
    error: str | None = None


@dataclass
class IngestReport:
    results: list[IndicatorResult] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    simulated: bool = False

    @property
    def errors(self) -> list[str]:
        out = [f"{r.indicator_id}: {r.error}" for r in self.results if r.error]
        for r in self.results:
            if r.summary:
                out += [f"{r.indicator_id}: {e}" for e in r.summary.errors]
        return out

    @property
    def quarantine(self) -> list[str]:
        return [
            f"{r.indicator_id}: {r.summary.quarantine}"
            for r in self.results
            if r.summary and r.summary.quarantine
        ]

    def needs_attention(self) -> bool:
        return bool(self.errors or self.quarantine or self.stale)

    def lines(self) -> list[str]:
        out = []
        for r in self.results:
            if r.status == "error":
                out.append(f"{r.indicator_id:24} ERROR: {r.error}")
            elif r.summary is None:
                out.append(f"{r.indicator_id:24} sin cambios")
            else:
                s = r.summary
                out.append(
                    f"{r.indicator_id:24} nuevas {s.new} · revisiones {s.revisions} · "
                    f"cuarentena {s.quarantine} · sin cambios {s.unchanged}"
                )
        out += [f"Desactualizado: {x}" for x in self.stale]
        return out


def _state(conn: psycopg.Connection, conector: str, recurso: str) -> str | None:
    row = conn.execute(
        "select hash_respuesta from ops.source_state where conector = %s and recurso_id = %s",
        (conector, recurso),
    ).fetchone()
    return row[0] if row else None


def _save_state(conn: psycopg.Connection, conector: str, recurso: str, digest: str) -> None:
    conn.execute(
        "insert into ops.source_state (conector, recurso_id, hash_respuesta, ultima_comprobacion)"
        " values (%s, %s, %s, now()) on conflict (conector, recurso_id) do update set"
        " hash_respuesta = excluded.hash_respuesta, ultima_comprobacion = now()",
        (conector, recurso, digest),
    )


def stale_indicators(
    conn: psycopg.Connection, specs: Iterable[IndicatorSpec], today: dt.date
) -> list[str]:
    """Indicators whose latest approved datum is older than the catalog allows."""
    out = []
    for spec in specs:
        row = conn.execute(
            "select max(periodo_fin) from ref.observations"
            " where indicator_id = %s and estado = 'aprobado'",
            (spec.id,),
        ).fetchone()
        last = row[0] if row else None
        if last is None:
            out.append(f"{spec.id}: sin datos aprobados")
        elif (today - last).days > spec.max_antiguedad_dias:
            out.append(
                f"{spec.id}: último dato del {last}, hace {(today - last).days} días"
                f" (máximo {spec.max_antiguedad_dias})"
            )
    return out


def ingest(
    conn: psycopg.Connection,
    catalog: dict[str, IndicatorSpec],
    client: httpx.Client,
    *,
    today: dt.date,
    only: set[str] | None = None,
    force: bool = False,
    simulate: bool = False,
    fetchers: dict[str, Fetcher] = FETCHERS,
) -> IngestReport:
    """Ingest the automatic indicators, one ingest run per connector. With `simulate`
    everything happens in the caller's transaction, which the caller rolls back."""
    report = IngestReport(simulated=simulate)
    specs = [
        s for s in catalog.values() if s.conector in AUTOMATIC and (only is None or s.id in only)
    ]
    by_connector: dict[str, list[IndicatorSpec]] = {}
    for s in specs:
        by_connector.setdefault(s.conector, []).append(s)

    for conector, group in by_connector.items():
        run_id = conn.execute(
            "insert into ops.ingest_runs (conector) values (%s) returning id", (conector,)
        ).fetchone()[0]
        results: list[IndicatorResult] = []
        for spec in group:
            try:
                ind = fetchers[conector](spec, client, today)
            except (IngestError, IndicatorUpdateError, httpx.HTTPError, ValueError) as exc:
                logger.warning("%s: %s", spec.id, exc)
                results.append(IndicatorResult(spec.id, "error", error=str(exc)))
                continue
            digest = fingerprint(ind)
            if not force and _state(conn, conector, spec.id) == digest:
                results.append(IndicatorResult(spec.id, "sin_cambios"))
                continue
            try:
                summary = load_series(
                    conn,
                    spec,
                    candidates_from_json(ind, spec),
                    run_id=run_id,
                    today=today,
                    curated=False,
                )
            except psycopg.Error as exc:
                raise IngestError(f"{spec.id}: la base rechazó la carga: {exc}") from exc
            _save_state(conn, conector, spec.id, digest)
            results.append(IndicatorResult(spec.id, "cargado", summary=summary))
        failed = [r for r in results if r.status == "error"]
        loaded = [r.summary for r in results if r.summary]
        conn.execute(
            "update ops.ingest_runs set fin = now(), estado = %s, filas_nuevas = %s,"
            " revisiones = %s, cuarentena = %s, errores = %s where id = %s",
            (
                "simulacion" if simulate else ("error" if failed and not loaded else "ok"),
                sum(s.new for s in loaded),
                sum(s.revisions for s in loaded),
                sum(s.quarantine for s in loaded),
                json.dumps([f"{r.indicator_id}: {r.error}" for r in failed], ensure_ascii=False),
                run_id,
            ),
        )
        report.results += results
    report.stale = stale_indicators(conn, specs, today)
    return report
