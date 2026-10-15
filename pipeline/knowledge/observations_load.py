"""Carga de series en ref.observations, siempre a través de validate.py.

La usa la carga inicial desde la base JSON (data/knowledge/indicators/) y la usarán los
conectores de ingesta. Cada carga queda registrada en ops.ingest_runs; una revisión del
dato oficial crea una versión nueva (primero se retira la vigente, después se inserta).
"""

import datetime as dt
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from pipeline.indicators import DEFAULT_INDICATORS_DIR, Indicator
from pipeline.knowledge.catalog import IndicatorSpec
from pipeline.knowledge.validate import Candidate, Current, Decision, validate_batch

# Ids of the JSON base → catalog ids.
JSON_TO_CATALOG = {
    "smi": "smi_mensual",
    "paro_epa": "paro_epa",
    "paro_juvenil_epa": "paro_juvenil_epa",
    "ipc_interanual": "ipc_interanual",
    "deuda_pib": "deuda_publica_pib",
    "deuda_absoluta": "deuda_publica_absoluta",
    "deficit_pib": "deficit_publico_pib",
    "pensiones": "num_pensiones",
    "pensionistas": "num_pensionistas",
    "poblacion": "poblacion",
    "pib": "pib",
    "pib_crecimiento": "pib_crecimiento",
    "afiliacion": "afiliacion_ss",
}
# Only the BOE gives the publication date of each value (that of its royal decree); the
# date of a statistical report is not the publication date of its historical values.
REAL_DATE_ORIGINS = {"boe"}


@dataclass
class LoadSummary:
    indicator_id: str
    new: int = 0
    revisions: int = 0
    unchanged: int = 0
    quarantine: int = 0
    pending: int = 0
    errors: list[str] = field(default_factory=list)


def period_label(start: dt.date, end: dt.date) -> str:
    """2018, 2013T1, 2022-07, 2024-01-01, or the explicit range for irregular periods."""
    if start.day == 1 and start.month == 1 and end == dt.date(start.year, 12, 31):
        return str(start.year)
    if start.day == 1 and start.year == end.year and (end + dt.timedelta(days=1)).day == 1:
        months = end.month - start.month + 1
        if months == 3 and start.month in (1, 4, 7, 10):
            return f"{start.year}T{(start.month - 1) // 3 + 1}"
        if months == 1:
            return f"{start:%Y-%m}"
    if start == end:
        return start.isoformat()
    return f"{start.isoformat()}/{end.isoformat()}"


def candidates_from_json(ind: Indicator, spec: IndicatorSpec) -> list[tuple[Candidate, dict]]:
    """Candidates with the source of each value (organismo, documento, url, fecha)."""
    real_dates = ind.origen in REAL_DATE_ORIGINS
    out = []
    for o in ind.serie:
        candidate = Candidate(
            indicator_id=spec.id,
            periodo=period_label(o.desde, o.hasta),
            periodo_inicio=o.desde,
            periodo_fin=o.hasta,
            valor=o.valor,
            unidad=ind.unidad,
            fecha_publicacion=o.fuente.fecha if real_dates else None,
        )
        out.append((candidate, o.fuente.model_dump()))
    return out


def _source_id(conn: psycopg.Connection, source: dict) -> int:
    row = conn.execute(
        "insert into ref.sources (organismo, titulo, url, fecha, tipo)"
        " values (%s, %s, %s, %s, 'primaria')"
        " on conflict (organismo, titulo, url) do update set organismo = excluded.organismo"
        " returning id",
        (source["organismo"], source["documento"], source["url"], source.get("fecha")),
    ).fetchone()
    return row[0]


def _stored(conn: psycopg.Connection, indicator_id: str):
    """Latest version per (periodo, territorio), and approved current values by start."""
    current = {
        (p, t): Current(valor=float(v), version=ver)
        for p, t, v, ver in conn.execute(
            "select distinct on (periodo, territorio_code) periodo, territorio_code, valor, version"
            " from ref.observations where indicator_id = %s"
            " order by periodo, territorio_code, version desc",
            (indicator_id,),
        ).fetchall()
    }
    previous = {
        (start, t): float(v)
        for start, t, v in conn.execute(
            "select periodo_inicio, territorio_code, valor from ref.observations"
            " where indicator_id = %s and vigente and estado = 'aprobado'",
            (indicator_id,),
        ).fetchall()
    }
    return current, previous


def load_series(
    conn: psycopg.Connection,
    spec: IndicatorSpec,
    rows: Sequence[tuple[Candidate, dict]],
    *,
    run_id: int,
    today: dt.date,
    curated: bool,
) -> LoadSummary:
    """Validate and store one indicator's values in the caller's transaction."""
    current, previous = _stored(conn, spec.id)
    sources = {(c.periodo, c.territorio_code): s for c, s in rows}
    decisions = validate_batch(
        [c for c, _ in rows],
        spec,
        current,
        today=today,
        curated=curated,
        previous_values=previous,
    )
    summary = LoadSummary(spec.id)
    for d in decisions:
        if d.action == "error":
            summary.errors.append(f"{d.candidate.periodo}: {'; '.join(d.motivos)}")
            continue
        if d.action == "sin_cambios":
            summary.unchanged += 1
            continue
        _store(conn, spec, d, sources[(d.candidate.periodo, d.candidate.territorio_code)], run_id)
        summary.new += d.action == "nueva"
        summary.revisions += d.action == "revision"
        summary.quarantine += d.estado == "cuarentena"
        summary.pending += d.estado == "pendiente"
    return summary


def _store(
    conn: psycopg.Connection, spec: IndicatorSpec, d: Decision, source: dict, run_id: int
) -> None:
    c = d.candidate
    if d.action == "revision":
        conn.execute(
            "update ref.observations set vigente = false where indicator_id = %s"
            " and periodo = %s and territorio_code = %s and vigente",
            (c.indicator_id, c.periodo, c.territorio_code),
        )
    conn.execute(
        "insert into ref.observations (indicator_id, periodo, periodo_inicio, periodo_fin,"
        " territorio_code, valor, unidad, fecha_publicacion, fecha_publicacion_estimada,"
        " version, vigente, estado, source_id, ingest_run_id, raw)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s, %s)",
        (
            c.indicator_id,
            c.periodo,
            c.periodo_inicio,
            c.periodo_fin,
            c.territorio_code,
            c.valor,
            spec.unidad,
            d.fecha_publicacion,
            d.fecha_publicacion_estimada,
            d.version,
            d.estado,
            _source_id(conn, source),
            run_id,
            json.dumps({"motivos": d.motivos}, ensure_ascii=False) if d.motivos else None,
        ),
    )


def load_json_base(
    conn: psycopg.Connection,
    catalog: dict[str, IndicatorSpec],
    *,
    today: dt.date,
    directory: Path = DEFAULT_INDICATORS_DIR,
) -> list[LoadSummary]:
    """Initial load of the JSON base, as one ingest run (committed by the caller)."""
    from pipeline.indicators import load_indicators

    base = load_indicators(directory)
    run_id = conn.execute(
        "insert into ops.ingest_runs (conector) values ('carga_inicial_json') returning id"
    ).fetchone()[0]
    summaries = []
    for json_id, catalog_id in JSON_TO_CATALOG.items():
        ind = base.indicators.get(json_id)
        if ind is None:
            continue
        spec = catalog[catalog_id]
        summaries.append(
            load_series(
                conn,
                spec,
                candidates_from_json(ind, spec),
                run_id=run_id,
                today=today,
                # Every source of the JSON base is an official primary one (BOE, INE,
                # Eurostat, INSS): it enters approved if it passes validation (decision of
                # the project owner, 2026-10-14). Quarantine still applies.
                curated=False,
            )
        )
    conn.execute(
        "update ops.ingest_runs set fin = now(), estado = 'ok', filas_nuevas = %s,"
        " revisiones = %s, cuarentena = %s, errores = %s where id = %s",
        (
            sum(s.new for s in summaries),
            sum(s.revisions for s in summaries),
            sum(s.quarantine for s in summaries),
            json.dumps([e for s in summaries for e in s.errors], ensure_ascii=False),
            run_id,
        ),
    )
    return summaries
