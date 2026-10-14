"""Trazabilidad: qué veredictos dependen de un dato (`uv run conocimiento impacto <id>`).

Cada veredicto guardado en verif.claims lleva los ids de las observaciones que usó. Si un
dato resulta erróneo o se revisa, se listan los veredictos afectados y el comando para
recalcular cada trabajo desde la evidencia (no se relanza solo: gasta búsquedas y modelo).
"""

import datetime as dt
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import psycopg

from pipeline.models import JobManifest

DATA_DIR = Path("data")


@dataclass(frozen=True)
class Dependent:
    job_id: str | None
    claim_id: str | None
    veredicto: str
    claim_normalizado: str
    fecha_verificacion: dt.datetime


def observation_summary(conn: psycopg.Connection, observation_id: int) -> str | None:
    row = conn.execute(
        "select indicator_id, periodo, territorio_code, valor::float8, unidad, version, estado"
        " from ref.observations where id = %s",
        (observation_id,),
    ).fetchone()
    if row is None:
        return None
    ind, periodo, terr, valor, unidad, version, estado = row
    return f"{ind} · {periodo} · {terr}: {valor:g} {unidad} (versión {version}, {estado})"


def dependents(conn: psycopg.Connection, observation_id: int) -> list[Dependent]:
    rows = conn.execute(
        "select job_id, claim_id, veredicto, claim_normalizado, fecha_verificacion"
        " from verif.claims where observation_ids @> %s::bigint[]"
        " order by fecha_verificacion desc",
        ([observation_id],),
    ).fetchall()
    return [Dependent(*r) for r in rows]


def reprocess_commands(job_ids: Iterable[str | None], data_dir: Path = DATA_DIR) -> list[str]:
    """`uv run verificar` from the evidence step for each job; a job whose folder is gone
    is reported instead (its input was deleted or processed elsewhere)."""
    commands = []
    for job_id in sorted({j for j in job_ids if j}):
        path = data_dir / job_id / "manifest.json"
        if not path.is_file():
            commands.append(f"# {job_id}: no está en {data_dir}/ (sin manifest)")
            continue
        m = JobManifest.model_validate_json(path.read_text(encoding="utf-8"))
        commands.append(
            f"uv run verificar {shlex.quote(m.input_path)} --fecha {m.speech_date:%Y-%m-%d}"
            " --from-step evidence"
        )
    return commands
