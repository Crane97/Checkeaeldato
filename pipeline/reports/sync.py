"""Guarda en Supabase un análisis hecho en data/<job_id>/ (desde el Mac o desde el worker).

Al empezar, el informe se registra `en_analisis` para que el panel lo vea. Al terminar se
suben a Storage los archivos de auditoría (nunca el vídeo ni el audio) y el informe HTML, y
después, en una transacción, el resultado como borrador y el registro de los archivos: la
base nunca apunta a un archivo que no se subió. Si el análisis falla, queda `error` con el
mensaje. Cada operación es una transacción corta: nada queda abierto mientras se analiza.
"""

import datetime as dt
from pathlib import Path

import psycopg

from pipeline.config import ConfigError, Settings
from pipeline.jobs import JOB_ID_LENGTH, file_sha256
from pipeline.models import InputKind, Report
from pipeline.report_html import render_report_html
from pipeline.reports.files import PRIVATE_BUCKET, Storage, StoredFile, record_file
from pipeline.reports.repository import (
    EventType,
    Origin,
    ReportMeta,
    create_report,
    load_meta,
    mark_error,
    store_result,
)

# Files of data/<job_id>/ kept to audit and re-evaluate a report -> their type.
AUDIT_FILES = {
    "transcript.json": "transcripcion",
    "speakers.json": "oradores",
    "speaker_research.json": "investigacion_oradores",
    "claims.json": "afirmaciones_detectadas",
    "context.json": "contexto",
    "evidence.json": "evidencia",
    "verdicts.json": "veredictos",
    "promises.json": "promesas",
    "report.json": "informe_json",
    "manifest.json": "manifest",
}


def job_id_for(path: Path) -> str:
    """Same id as JobStore: the first characters of the input file's SHA-256."""
    return file_sha256(path)[:JOB_ID_LENGTH]


class ReportUploader:
    def __init__(self, conn: psycopg.Connection, storage: Storage, origen: Origin) -> None:
        self._conn = conn
        self._storage = storage
        self._origen = origen

    @classmethod
    def open(cls, settings: Settings, origen: Origin) -> "ReportUploader":
        """Fails at once, before analysing anything, when Supabase is not configured."""
        if settings.supabase_db_url is None:
            raise ConfigError("Falta SUPABASE_DB_URL en .env para subir el informe")
        storage = Storage.from_settings(settings)
        conn = psycopg.connect(settings.supabase_db_url.get_secret_value(), connect_timeout=20)
        return cls(conn, storage, origen)

    def close(self) -> None:
        self._conn.close()

    def begin(
        self,
        *,
        job_id: str,
        titulo: str,
        fecha: dt.date,
        input_kind: InputKind,
        fuente: str,
        tipo: EventType | None = None,
    ) -> ReportMeta:
        with self._conn.transaction():
            return create_report(
                self._conn, job_id=job_id, titulo=titulo, fecha=fecha, input_kind=input_kind,
                fuente=fuente, origen=self._origen, tipo=tipo,
            )  # fmt: skip

    def fail(self, job_id: str, message: str) -> None:
        with self._conn.transaction():
            mark_error(self._conn, job_id, message)

    def finish(self, job_dir: Path, report: Report) -> ReportMeta:
        self._storage.ensure_bucket(PRIVATE_BUCKET, public=False)
        uploaded: list[tuple[str, StoredFile]] = []
        for name, tipo in AUDIT_FILES.items():
            path = job_dir / name
            if path.is_file():
                stored = self._storage.upload(
                    PRIVATE_BUCKET, f"{report.job_id}/{name}", path.read_bytes(),
                    "application/json",
                )  # fmt: skip
                uploaded.append((tipo, stored))
        html = render_report_html(report).encode()
        stored = self._storage.upload(
            PRIVATE_BUCKET, f"{report.job_id}/informe.html", html, "text/html; charset=utf-8"
        )
        uploaded.append(("informe_html", stored))
        with self._conn.transaction():
            store_result(self._conn, report)
            for tipo, item in uploaded:
                record_file(self._conn, report.job_id, tipo, item)
            return load_meta(self._conn, report.job_id)
