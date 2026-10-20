"""Informes en Postgres (esquema app, migración 0011).

Un informe se crea al lanzar el análisis (`en_analisis`), recibe el resultado al terminar
(`borrador`) o el error. Cada afirmación y promesa se guarda como versión en
app.claim_versions; el informe se reconstruye con las versiones vigentes, en su orden. Volver
a analizar un borrador añade versiones solo donde el resultado cambió y deja de mostrar lo
que ya no aparece: nada se borra.
"""

import datetime as dt
import json
import re
import unicodedata
from typing import Any, Literal

import psycopg
from pydantic import BaseModel

from pipeline.models import Claim, InputKind, Promise, Report

EventType = Literal["debate", "mitin", "entrevista", "sesion_parlamentaria"]
ReportState = Literal["en_analisis", "borrador", "publicado", "retirado", "error"]
Origin = Literal["servidor", "local"]
MAX_SLUG_WORDS = 9
# What the Report model keeps outside the claim rows (identity columns and computed fields
# are left out: they live in their own columns or are recalculated).
_META_EXCLUDE = {
    "job_id", "source", "input_kind", "speech_date", "claims", "promises",
    "by_verdict", "by_basis", "by_category", "by_promise", "by_speaker",
}  # fmt: skip
_REANALYSABLE = ("en_analisis", "borrador", "error")


class ReportError(Exception):
    """The report does not exist or its state does not allow the operation."""


class ReportMeta(BaseModel):
    id: str
    slug: str
    titulo: str
    entradilla: str | None
    tipo: EventType | None
    fecha_discurso: dt.date
    input_kind: InputKind
    fuente: str
    origen: Origin
    estado: ReportState
    error: str | None
    progreso: dict[str, Any]
    creado_en: dt.datetime
    actualizado_en: dt.datetime
    analizado_en: dt.datetime | None
    publicado_en: dt.datetime | None
    retirado_en: dt.datetime | None
    nota_retirada: str | None


_META_COLUMNS = ", ".join(ReportMeta.model_fields)


def slugify(fecha: dt.date, titulo: str) -> str:
    """`2026-10-18-debate-a-tres`: date plus up to nine words, ASCII, lower case."""
    ascii_title = unicodedata.normalize("NFKD", titulo).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_title.lower())[:MAX_SLUG_WORDS]
    return f"{fecha:%Y-%m-%d}-{'-'.join(words) or 'informe'}"


def _unique_slug(conn: psycopg.Connection, base: str, job_id: str) -> str:
    taken = {
        row[0]
        for row in conn.execute(
            "select slug from app.reports where (slug = %s or slug like %s) and id <> %s",
            (base, f"{base}-%", job_id),
        ).fetchall()
    }
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def load_meta(conn: psycopg.Connection, job_id: str) -> ReportMeta:
    row = conn.execute(f"select {_META_COLUMNS} from app.reports where id = %s", (job_id,))
    found = row.fetchone()
    if found is None:
        raise ReportError(f"El informe {job_id} no existe")
    return ReportMeta(**dict(zip(ReportMeta.model_fields, found, strict=True)))


def create_report(
    conn: psycopg.Connection,
    *,
    job_id: str,
    titulo: str,
    fecha: dt.date,
    input_kind: InputKind,
    fuente: str,
    origen: Origin,
    tipo: EventType | None = None,
    entradilla: str | None = None,
) -> ReportMeta:
    """Register an analysis that starts now. Running again a draft or a failed analysis
    reuses its row (and its slug); a published or withdrawn report is never touched."""
    existing = conn.execute(
        "select estado from app.reports where id = %s for update", (job_id,)
    ).fetchone()
    if existing is not None:
        if existing[0] not in _REANALYSABLE:
            raise ReportError(
                f"El informe {job_id} está {existing[0]}: no se vuelve a analizar"
                " (publicado o retirado)"
            )
        conn.execute(
            "update app.reports set estado = 'en_analisis', error = null, progreso = '{}',"
            " titulo = %s, tipo = coalesce(%s, tipo), entradilla = coalesce(%s, entradilla),"
            " origen = %s where id = %s",
            (titulo, tipo, entradilla, origen, job_id),
        )
        return load_meta(conn, job_id)
    slug = _unique_slug(conn, slugify(fecha, titulo), job_id)
    conn.execute(
        "insert into app.reports (id, slug, titulo, entradilla, tipo, fecha_discurso,"
        " input_kind, fuente, origen) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (job_id, slug, titulo, entradilla, tipo, fecha, input_kind, fuente, origen),
    )
    return load_meta(conn, job_id)


def set_progress(conn: psycopg.Connection, job_id: str, progreso: dict[str, Any]) -> None:
    conn.execute(
        "update app.reports set progreso = %s where id = %s",
        (json.dumps(progreso, ensure_ascii=False), job_id),
    )


def mark_error(conn: psycopg.Connection, job_id: str, message: str) -> None:
    cur = conn.execute(
        "update app.reports set estado = 'error', error = %s"
        " where id = %s and estado in ('en_analisis', 'borrador', 'error')",
        (message.strip() or "Error sin detalle", job_id),
    )
    if cur.rowcount != 1:
        raise ReportError(f"El informe {job_id} no existe o ya está publicado")


def _speaker(report: Report, label: str) -> tuple[str, str | None]:
    profile = next((s for s in report.speaker_profiles if s.label == label), None)
    if profile is None:
        return report.speaker_name(label), None
    return profile.name or report.speaker_name(label), profile.party


def _rows(report: Report) -> list[tuple[int, Claim | Promise]]:
    items: list[Claim | Promise] = [*report.claims, *report.promises]
    return list(enumerate(items))


def store_result(conn: psycopg.Connection, report: Report) -> None:
    """Save a finished analysis as a draft, adding a version only where something changed."""
    meta = load_meta(conn, report.job_id)
    if meta.estado not in _REANALYSABLE:
        raise ReportError(f"El informe {report.job_id} está {meta.estado}: no se sobrescribe")
    current = {
        claim_id: (version, contenido)
        for claim_id, version, contenido in conn.execute(
            "select claim_id, version, contenido from app.claim_versions"
            " where report_id = %s and vigente",
            (report.job_id,),
        ).fetchall()
    }
    latest = dict(
        conn.execute(
            "select claim_id, max(version) from app.claim_versions where report_id = %s"
            " group by claim_id",
            (report.job_id,),
        ).fetchall()
    )
    seen: set[str] = set()
    for orden, item in _rows(report):
        seen.add(item.id)
        content = item.model_dump(mode="json")
        if item.id in current and current[item.id][1] == content:
            continue
        if item.id in current:
            conn.execute(
                "update app.claim_versions set vigente = false"
                " where report_id = %s and claim_id = %s and vigente",
                (report.job_id, item.id),
            )
        name, party = _speaker(report, item.speaker)
        is_claim = isinstance(item, Claim)
        conn.execute(
            "insert into app.claim_versions (report_id, claim_id, orden, kind, version, vigente,"
            " origen, etiqueta, categoria, orador, partido, confianza, contenido)"
            " values (%s, %s, %s, %s, %s, true, 'analisis', %s, %s, %s, %s, %s, %s)",
            (
                report.job_id, item.id, orden, item.kind, latest.get(item.id, 0) + 1,
                item.verdict if is_claim else item.assessment,
                item.category if is_claim else None,
                name, party, item.confidence, json.dumps(content, ensure_ascii=False),
            ),
        )  # fmt: skip
    gone = set(current) - seen
    if gone:
        conn.execute(
            "update app.claim_versions set vigente = false"
            " where report_id = %s and vigente and claim_id = any(%s)",
            (report.job_id, sorted(gone)),
        )
    conn.execute(
        "update app.reports set estado = 'borrador', error = null, analizado_en = now(),"
        " meta = %s where id = %s",
        (report.model_dump_json(exclude=_META_EXCLUDE), report.job_id),
    )


def load_report(conn: psycopg.Connection, job_id: str) -> Report:
    """The report as it stands: its current version of every claim and promise."""
    row = conn.execute(
        "select fuente, input_kind, fecha_discurso, meta from app.reports where id = %s",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ReportError(f"El informe {job_id} no existe")
    fuente, input_kind, fecha, meta = row
    claims, promises = [], []
    for kind, contenido in conn.execute(
        "select kind, contenido from app.claim_versions where report_id = %s and vigente"
        " order by orden, claim_id",
        (job_id,),
    ).fetchall():
        if kind == "promesa":
            promises.append(Promise.model_validate(contenido))
        else:
            claims.append(Claim.model_validate(contenido))
    return Report.model_validate(
        {
            **meta,
            "job_id": job_id,
            "source": fuente,
            "input_kind": input_kind,
            "speech_date": fecha,
            "claims": claims,
            "promises": promises,
        }
    )
