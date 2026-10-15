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
from pipeline.knowledge.catalog import IndicatorSpec, TerritorialSource
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


# --- Territorial series (fase H) ----------------------------------------------

INE_TABLE_DATA = "https://servicios.ine.es/wstempus/js/ES/DATOS_TABLA/{tabla}"
INE_OPERATION_TABLES = "https://servicios.ine.es/wstempus/js/ES/TABLAS_OPERACION/{op}"
PADRON_PROVINCE_TABLE = ": Población por municipios y sexo."


def _json(client: httpx.Client, url: str, **params) -> list | dict:
    response = client.get(url, params=params, timeout=120)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and "status" in data:
        raise IngestError(f"INE: {data['status']}")
    return data


def _parts(name: str) -> list[str]:
    return [p.strip() for p in name.split(".") if p.strip()]


def territorial_series(
    src: TerritorialSource, client: httpx.Client, territories
) -> tuple[list[tuple[str, str, int]], list[str]]:
    """[(INE series code, territory code, table)] for one territorial source, and the
    names that could not be matched to exactly one territory (reported, not guessed)."""
    from pipeline.knowledge.territories import resolve

    found: list[tuple[str, str, int]] = []
    unmatched: list[str] = []
    if src.padron_provincial:
        tables = [
            t["Id"]
            for t in _json(client, INE_OPERATION_TABLES.format(op="DPOP"))
            if t["Nombre"].strip().endswith(PADRON_PROVINCE_TABLE)
        ]
        for table in tables:
            rows = _json(client, INE_TABLE_DATA.format(tabla=table), nult=1)
            first = rows[0]  # the province total comes first
            parts = _parts(first["Nombre"])
            code = resolve(parts[0], territories, "provincia") if len(parts) > 1 else None
            if code is None or parts[1] != "Total":
                unmatched.append(first["Nombre"])
            else:
                found.append((first["COD"], code, table))
        return found, unmatched
    wanted = set(src.filtro)
    for row in _json(client, INE_TABLE_DATA.format(tabla=src.tabla), nult=1):
        parts = _parts(row["Nombre"])
        if not wanted <= set(parts):
            continue
        rest = [p for p in parts if p not in wanted]
        if len(rest) != 1 or rest[0].lower().startswith("total"):
            continue
        code = resolve(rest[0], territories, src.nivel)
        if code is None:
            unmatched.append(row["Nombre"])
        else:
            found.append((row["COD"], code, src.tabla))  # type: ignore[arg-type]
    return found, unmatched


def territorial_candidates(
    spec: IndicatorSpec,
    src: TerritorialSource,
    client: httpx.Client,
    today: dt.date,
    territories,
) -> tuple[list, list[str]]:
    """Validated-ready candidates of every territory of one source, and problems."""
    from dataclasses import replace

    series, problems = territorial_series(src, client, territories)
    problems = [f"sin territorio único: {n}" for n in problems]
    periodicity = (
        spec.frecuencia if spec.frecuencia in ("mensual", "trimestral", "anual") else "puntual"
    )
    rows = []
    for cod, code, table in series:
        ine = IneSpec(
            id=spec.id, nombre=spec.nombre, unidad=spec.unidad, cod=cod, tabla=table,
            documento=spec.nombre, periodicidad=periodicity,  # type: ignore[arg-type]
        )  # fmt: skip
        try:
            ind = build_ine(ine, client, today=today)
        except (IndicatorUpdateError, httpx.HTTPError, ValueError) as exc:
            problems.append(f"{code} ({cod}): {exc}")
            continue
        rows += [
            (replace(c, territorio_code=code), source)
            for c, source in candidates_from_json(ind, spec)
        ]
    return rows, problems


def ingest_territorial(
    conn: psycopg.Connection,
    catalog: dict[str, IndicatorSpec],
    client: httpx.Client,
    *,
    today: dt.date,
    territories,
    only: set[str] | None = None,
    force: bool = False,
    simulate: bool = False,
) -> IngestReport:
    """Series by region and province (the switch `ingerir --territorial`): one ingest run;
    each source is validated with its own plausible range and skipped if unchanged."""
    report = IngestReport(simulated=simulate)
    run_id = conn.execute(
        "insert into ops.ingest_runs (conector) values ('ine_territorial') returning id"
    ).fetchone()[0]
    loaded: list[LoadSummary] = []
    for spec in catalog.values():
        if not spec.territorial or (only is not None and spec.id not in only):
            continue
        for src in spec.territorial:
            label = f"{spec.id}@{src.nivel}"
            try:
                rows, problems = territorial_candidates(spec, src, client, today, territories)
            except (IngestError, httpx.HTTPError, ValueError) as exc:
                report.results.append(IndicatorResult(label, "error", error=str(exc)))
                continue
            expected = sum(1 for t in territories if t.nivel == src.nivel)
            got = {c.territorio_code for c, _ in rows}
            if rows and len(got) < expected:
                missing = sorted(
                    t.nombre for t in territories if t.nivel == src.nivel and t.code not in got
                )
                problems.append(f"faltan {expected - len(got)} territorios: {', '.join(missing)}")
            digest = hashlib.sha256(
                json.dumps(
                    sorted((c.territorio_code, c.periodo, c.valor) for c, _ in rows)
                ).encode()
            ).hexdigest()
            if not rows:
                report.results.append(
                    IndicatorResult(label, "error", error="; ".join(problems) or "sin series")
                )
                continue
            if not force and _state(conn, "ine_territorial", label) == digest:
                report.results.append(IndicatorResult(label, "sin_cambios"))
                continue
            limits: dict = {"rango_min": src.rango_min, "rango_max": src.rango_max}
            if src.salto_max_puntos or src.salto_max_pct:
                limits |= {
                    "salto_max_puntos": src.salto_max_puntos,
                    "salto_max_pct": src.salto_max_pct,
                }
            ranged = spec.model_copy(update=limits)
            summary = load_series(conn, ranged, rows, run_id=run_id, today=today, curated=False)
            summary.errors += problems
            loaded.append(summary)
            _save_state(conn, "ine_territorial", label, digest)
            report.results.append(IndicatorResult(label, "cargado", summary=summary))
    conn.execute(
        "update ops.ingest_runs set fin = now(), estado = %s, filas_nuevas = %s,"
        " revisiones = %s, cuarentena = %s, errores = %s where id = %s",
        (
            "simulacion" if simulate else "ok",
            sum(s.new for s in loaded),
            sum(s.revisions for s in loaded),
            sum(s.quarantine for s in loaded),
            json.dumps(report.errors, ensure_ascii=False),
            run_id,
        ),
    )
    return report
