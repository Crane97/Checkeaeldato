"""Sube a Supabase los informes y la caché de búsquedas que hay en data/ (una vez).

Cada carpeta data/<job_id>/ con un informe terminado se guarda como borrador, con sus
archivos de auditoría, igual que `verificar --subir`. Lo que ya está en Supabase no se toca
(se puede repetir sin duplicar). Los informes antiguos no tienen título ni tipo: el título
sale del nombre del archivo y el tipo se completa en el panel antes de publicar.
"""

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from pydantic import ValidationError

from pipeline.evidence.search_cache import EntryStore
from pipeline.models import JobManifest, Report
from pipeline.reports.sync import ReportUploader

logger = logging.getLogger(__name__)

_JOB_DIR = re.compile(r"^[0-9a-f]{12}$")
_CACHE_FILE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class MigrationReport:
    uploaded: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    unfinished: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cache_copied: int = 0
    cache_present: int = 0


def title_from_source(source: str) -> str:
    """`debate_a_tres-2026.txt` → `Debate a tres 2026`."""
    words = re.sub(r"[_\-.]+", " ", Path(source).stem).split()
    return " ".join(words).capitalize() or "Informe"


def finished_jobs(data_dir: Path) -> list[tuple[Path, Report | None]]:
    """Job folders, with their report when the analysis finished."""
    found = []
    for job_dir in sorted(p for p in data_dir.iterdir() if _JOB_DIR.match(p.name)):
        manifest_path, report_path = job_dir / "manifest.json", job_dir / "report.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = JobManifest.model_validate_json(manifest_path.read_text())
            done = manifest.steps.get("report")
            if done is None or done.status != "done" or not report_path.is_file():
                found.append((job_dir, None))
                continue
            found.append((job_dir, Report.model_validate_json(report_path.read_text())))
        except (OSError, ValidationError) as exc:
            logger.warning("%s ilegible, se omite: %s", job_dir.name, exc)
            found.append((job_dir, None))
    return found


def migrate_reports(
    conn: psycopg.Connection,
    uploader: ReportUploader,
    data_dir: Path,
    *,
    simulate: bool = False,
) -> MigrationReport:
    result = MigrationReport()
    for job_dir, report in finished_jobs(data_dir):
        if report is None:
            result.unfinished.append(job_dir.name)
            continue
        exists = conn.execute(
            "select 1 from app.reports where id = %s", (report.job_id,)
        ).fetchone()
        if exists:
            result.already.append(report.job_id)
            continue
        if simulate:
            result.uploaded.append(report.job_id)
            continue
        try:
            uploader.begin(
                job_id=report.job_id, titulo=title_from_source(report.source),
                fecha=report.speech_date, input_kind=report.input_kind, fuente=report.source,
            )  # fmt: skip
            uploader.finish(job_dir, report)
        except Exception as exc:  # one bad folder does not stop the others; reported
            logger.warning("No se pudo subir %s: %s", report.job_id, exc)
            result.failed.append(f"{report.job_id}: {exc}")
            continue
        result.uploaded.append(report.job_id)
    return result


def migrate_search_cache(
    store: EntryStore, cache_dir: Path, result: MigrationReport, *, simulate: bool = False
) -> None:
    """Copy disk entries the database does not have yet, keeping their date."""
    if not cache_dir.is_dir():
        return
    for path in sorted(cache_dir.glob("*/*.json")):
        if not _CACHE_FILE.match(path.stem):
            continue
        if store.read(path.stem) is not None:
            result.cache_present += 1
            continue
        try:
            entry = json.loads(path.read_text())
            saved_at = dt.datetime.fromisoformat(entry["saved_at"])
            if not simulate:
                store.write(path.stem, entry["request"], entry["response"], saved_at)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Entrada de caché ilegible %s, se omite: %s", path, exc)
            continue
        result.cache_copied += 1
