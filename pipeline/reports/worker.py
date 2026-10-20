"""Cola de análisis en Postgres (app.analysis_queue) y worker en segundo plano.

El panel encola un análisis (`enqueue`): se crea el informe `en_analisis` y una fila
`pendiente` con todo lo necesario para ejecutarlo, también tras un reinicio. Cada hilo del
worker toma el trabajo más antiguo con FOR UPDATE SKIP LOCKED (nunca dos hilos el mismo),
lo ejecuta en data/<job_id>/, guarda el paso en curso en el informe y, al terminar, sube el
resultado como borrador (ReportUploader). Lo que un reinicio dejó a medias se marca
`interrumpido` y el informe `error`, con un mensaje que dice que se puede reintentar.
"""

import datetime as dt
import hashlib
import logging
import threading
from collections.abc import Callable
from pathlib import Path

import psycopg
from pydantic import BaseModel, Field

from pipeline.jobs import JOB_ID_LENGTH
from pipeline.models import STEPS, StepName
from pipeline.reports.files import Storage
from pipeline.reports.repository import EventType, ReportError, create_report, set_progress
from pipeline.reports.sync import ReportUploader
from pipeline.run import Deps, run_job
from pipeline.speakers import parse_speaker_lines

logger = logging.getLogger(__name__)

POLL_SECONDS = 3.0
INTERRUPTED = "Análisis interrumpido por un reinicio de la aplicación: se puede reintentar"


class AnalysisRequest(BaseModel):
    """Everything needed to run an analysis, stored in the queue row."""

    titulo: str = Field(min_length=1)
    tipo: EventType | None = None
    fecha: dt.date
    texto: str = Field(min_length=1)
    autor: str | None = None
    oradores: str | None = None  # "Nombre | Partido | Rol", one per line

    def input_text(self) -> str:
        return self.texto.strip() + "\n"

    def job_id(self) -> str:
        """Same id the job folder gets from the input file's SHA-256."""
        return hashlib.sha256(self.input_text().encode()).hexdigest()[:JOB_ID_LENGTH]


class QueueItem(BaseModel):
    id: int
    report_id: str
    solicitud: AnalysisRequest


def enqueue(conn: psycopg.Connection, request: AnalysisRequest) -> str:
    """Register the report and queue its analysis, in one transaction."""
    job_id = request.job_id()
    with conn.transaction():
        busy = conn.execute(
            "select 1 from app.analysis_queue where report_id = %s"
            " and estado in ('pendiente', 'en_curso')",
            (job_id,),
        ).fetchone()
        if busy:
            raise ReportError("Ese texto ya se está analizando o está en cola")
        create_report(
            conn, job_id=job_id, titulo=request.titulo, fecha=request.fecha, input_kind="texto",
            fuente=f"{job_id}.txt", origen="servidor", tipo=request.tipo,
        )  # fmt: skip
        conn.execute(
            "insert into app.analysis_queue (report_id, solicitud) values (%s, %s)",
            (job_id, request.model_dump_json()),
        )
    return job_id


def claim_next(conn: psycopg.Connection, worker: str) -> QueueItem | None:
    """Take the oldest pending analysis, unless another thread already holds it."""
    with conn.transaction():
        row = conn.execute(
            "select id, report_id, solicitud from app.analysis_queue where estado = 'pendiente'"
            " order by creado_en, id limit 1 for update skip locked"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "update app.analysis_queue set estado = 'en_curso', worker = %s,"
            " intentos = intentos + 1, empezado_en = now() where id = %s",
            (worker, row[0]),
        )
    return QueueItem(id=row[0], report_id=row[1], solicitud=row[2])


def recover_interrupted(conn: psycopg.Connection) -> int:
    """At start-up nothing can be running: mark what a restart cut off."""
    with conn.transaction():
        ids = [
            r[0]
            for r in conn.execute(
                "update app.analysis_queue set estado = 'interrumpido', terminado_en = now()"
                " where estado = 'en_curso' returning report_id"
            ).fetchall()
        ]
        if ids:
            conn.execute(
                "update app.reports set estado = 'error', error = %s"
                " where id = any(%s) and estado = 'en_analisis'",
                (INTERRUPTED, ids),
            )
    return len(ids)


def _finish_item(conn: psycopg.Connection, item_id: int, estado: str) -> None:
    with conn.transaction():
        conn.execute(
            "update app.analysis_queue set estado = %s, terminado_en = now() where id = %s",
            (estado, item_id),
        )


def process(
    conn: psycopg.Connection,
    storage: Storage,
    item: QueueItem,
    *,
    deps: Deps,
    data_dir: Path,
) -> bool:
    """Run one queued analysis. True if it ended as a draft; the error is recorded if not."""
    request = item.solicitud

    def on_step(step: StepName) -> None:
        progress = {"paso": step, "numero": STEPS.index(step) + 1, "total": len(STEPS)}
        with conn.transaction():
            set_progress(conn, item.report_id, progress)

    uploader = ReportUploader(conn, storage, origen="servidor")
    try:
        inputs = data_dir / "entradas"
        inputs.mkdir(parents=True, exist_ok=True)
        path = inputs / f"{item.report_id}.txt"
        path.write_text(request.input_text(), encoding="utf-8")
        result = run_job(
            path,
            speech_date=request.fecha,
            data_dir=data_dir,
            deps=deps,
            author=(request.autor or "").strip() or None,
            speaker_info=parse_speaker_lines(request.oradores) if request.oradores else None,
            on_step=on_step,
        )
        uploader.finish(data_dir / item.report_id, result.report)
    except Exception as exc:  # recorded on the report and in the log, never swallowed
        logger.exception("El análisis %s falló", item.report_id)
        uploader.fail(item.report_id, str(exc) or type(exc).__name__)
        _finish_item(conn, item.id, "error")
        return False
    _finish_item(conn, item.id, "hecho")
    return True


class Worker:
    """`concurrency` threads that take queued analyses until stopped."""

    def __init__(
        self,
        connect: Callable[[], psycopg.Connection],
        storage: Storage,
        deps_factory: Callable[[], Deps],
        data_dir: Path,
        concurrency: int = 1,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._connect = connect
        self._storage = storage
        self._deps_factory = deps_factory
        self._data_dir = data_dir
        self._concurrency = concurrency
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        with self._connect() as conn:
            if n := recover_interrupted(conn):
                logger.warning("%d análisis interrumpidos por el reinicio: se pueden reintentar", n)
        for i in range(self._concurrency):
            thread = threading.Thread(
                target=self._loop, args=(f"worker-{i}",), daemon=True, name=f"analisis-{i}"
            )
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout)

    def run_once(self, name: str) -> bool:
        """Process one queued analysis if there is any. True if one was processed."""
        with self._connect() as conn:
            item = claim_next(conn, name)
            if item is None:
                return False
            process(conn, self._storage, item, deps=self._deps_factory(), data_dir=self._data_dir)
            return True

    def _loop(self, name: str) -> None:
        while not self._stop.is_set():
            try:
                busy = self.run_once(name)
            except Exception:  # database down, etc.: logged and retried after a pause
                logger.exception("El worker %s no pudo consultar la cola", name)
                busy = False
            if not busy:
                self._stop.wait(self._poll)
