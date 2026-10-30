"""Cola de análisis en Postgres (app.analysis_queue) y worker en segundo plano.

El panel encola un análisis (`enqueue`): se crea el informe `en_analisis` y una fila
`pendiente` con todo lo necesario para ejecutarlo, también tras un reinicio. Cada hilo del
worker toma el trabajo más antiguo con FOR UPDATE SKIP LOCKED (nunca dos hilos el mismo),
lo ejecuta en data/<job_id>/, guarda el paso en curso en el informe y, al terminar, sube el
resultado como borrador (ReportUploader).

Un análisis dura minutos: no se mantiene abierta ninguna conexión mientras tanto. Cada
actualización (progreso, latido, resultado, error) usa una conexión corta, y un fallo al
anotar el progreso o el latido no corta el análisis. Mientras analiza, el worker renueva el
latido del trabajo; uno `en_curso` sin latido reciente es de un proceso que ya no existe y
se marca `interrumpido` (al arrancar y periódicamente), sin tocar los de otro proceso vivo.
"""

import datetime as dt
import hashlib
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path

import psycopg
from pydantic import BaseModel, Field

from pipeline.jobs import JOB_ID_LENGTH
from pipeline.models import STEPS, StepName
from pipeline.reports.files import Storage
from pipeline.reports.repository import (
    EventType,
    ReportError,
    create_report,
    mark_error,
    set_progress,
)
from pipeline.reports.safe_errors import public_message
from pipeline.reports.sync import ReportUploader
from pipeline.run import Deps, run_job
from pipeline.speakers import parse_speaker_lines

logger = logging.getLogger(__name__)

POLL_SECONDS = 3.0
HEARTBEAT_SECONDS = 60.0
# Without a heartbeat for this long, the process that ran it is gone.
STALE_AFTER = dt.timedelta(minutes=10)
RECOVER_EVERY_SECONDS = 120.0
FINAL_UPDATE_ATTEMPTS = 3
INTERRUPTED = "Análisis interrumpido (el proceso que lo ejecutaba se detuvo): se puede reintentar"

Connect = Callable[[], AbstractContextManager[psycopg.Connection]]


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


def worker_name(thread: int) -> str:
    """Machine, process and thread: tells apart the workers of two processes."""
    return f"{socket.gethostname()}:{os.getpid()}:{thread}"


def enqueue(conn: psycopg.Connection, request: AnalysisRequest) -> str:
    """Register the report and queue its analysis, in one transaction."""
    job_id = request.job_id()
    try:
        with conn.transaction():
            busy = conn.execute(
                "select 1 from app.analysis_queue where report_id = %s"
                " and estado in ('pendiente', 'en_curso')",
                (job_id,),
            ).fetchone()
            if busy:
                raise ReportError("Ese texto ya se está analizando o está en cola")
            create_report(
                conn, job_id=job_id, titulo=request.titulo, fecha=request.fecha,
                input_kind="texto", fuente=f"{job_id}.txt", origen="servidor", tipo=request.tipo,
            )  # fmt: skip
            conn.execute(
                "insert into app.analysis_queue (report_id, solicitud) values (%s, %s)",
                (job_id, request.model_dump_json()),
            )
    except psycopg.errors.UniqueViolation as exc:
        # Another request with the same text (or the same title and date) got there first.
        raise ReportError(
            "Otro análisis igual se registró a la vez: ya está en cola o vuelve a intentarlo"
        ) from exc
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
            " intentos = intentos + 1, empezado_en = now(), latido_en = now() where id = %s",
            (worker, row[0]),
        )
    return QueueItem(id=row[0], report_id=row[1], solicitud=row[2])


def heartbeat(conn: psycopg.Connection, item_id: int) -> None:
    with conn.transaction():
        conn.execute(
            "update app.analysis_queue set latido_en = now() where id = %s and estado = 'en_curso'",
            (item_id,),
        )


def recover_interrupted(conn: psycopg.Connection, stale_after: dt.timedelta = STALE_AFTER) -> int:
    """Mark as interrupted what has had no heartbeat for `stale_after` (its process is gone);
    the analyses of a live process keep beating and are left alone."""
    with conn.transaction():
        ids = [
            r[0]
            for r in conn.execute(
                "update app.analysis_queue set estado = 'interrumpido', terminado_en = now()"
                " where estado = 'en_curso' and latido_en <= now() - %s returning report_id",
                (stale_after,),
            ).fetchall()
        ]
        if ids:
            conn.execute(
                "update app.reports set estado = 'error', error = %s"
                " where id = any(%s) and estado = 'en_analisis'",
                (INTERRUPTED, ids),
            )
    return len(ids)


def _still_running(conn: psycopg.Connection, item_id: int) -> None:
    """Lock the item, in the caller's transaction, and fail if it is no longer running: a
    worker whose heartbeat was missed (network cut, sleeping machine) was declared
    interrupted and must not overwrite what a retry may be writing."""
    row = conn.execute(
        "select 1 from app.analysis_queue where id = %s and estado = 'en_curso' for update",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ReportError(
            f"El trabajo {item_id} se dio por interrumpido mientras se analizaba: no se guarda"
        )


def _finish_item(conn: psycopg.Connection, item_id: int, estado: str) -> None:
    """Close the item (in the caller's transaction, which already locked it)."""
    conn.execute(
        "update app.analysis_queue set estado = %s, terminado_en = now()"
        " where id = %s and estado = 'en_curso'",
        (estado, item_id),
    )


def _with_retries(connect: Connect, action: Callable[[psycopg.Connection], None]) -> None:
    """A final update on a fresh connection, retried: the long analysis may have outlived
    the previous one. Raises after the last attempt (the stale check then recovers it)."""
    for attempt in range(1, FINAL_UPDATE_ATTEMPTS + 1):
        try:
            with connect() as conn:
                action(conn)
            return
        except psycopg.OperationalError:
            if attempt == FINAL_UPDATE_ATTEMPTS:
                raise
            time.sleep(attempt)


class _Heartbeat:
    """Renews the item's heartbeat every `seconds` until stopped (best effort)."""

    def __init__(self, connect: Connect, item_id: int, seconds: float) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(connect, item_id, seconds), daemon=True, name="latido"
        )

    def _run(self, connect: Connect, item_id: int, seconds: float) -> None:
        while not self._stop.wait(seconds):
            try:
                with connect() as conn:
                    heartbeat(conn, item_id)
            except Exception:  # noqa: BLE001 — a missed beat is retried; the analysis goes on
                logger.warning("No se pudo renovar el latido del análisis %s", item_id)

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def process(
    connect: Connect,
    storage: Storage,
    item: QueueItem,
    *,
    deps_factory: Callable[[], Deps],
    data_dir: Path,
    secrets: Sequence[str] = (),
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> bool:
    """Run one queued analysis. True if it ended as a draft; the error is recorded if not."""
    request = item.solicitud

    def on_step(step: StepName) -> None:
        progress = {"paso": step, "numero": STEPS.index(step) + 1, "total": len(STEPS)}
        try:
            with connect() as conn, conn.transaction():
                set_progress(conn, item.report_id, progress)
                conn.execute(
                    "update app.analysis_queue set latido_en = now() where id = %s", (item.id,)
                )
        except psycopg.Error:  # progress is informative: the analysis goes on
            logger.warning("No se pudo anotar el progreso de %s (%s)", item.report_id, step)

    def store(conn: psycopg.Connection) -> None:
        def guard(c: psycopg.Connection) -> None:
            _still_running(c, item.id)
            _finish_item(c, item.id, "hecho")  # same transaction as the result

        with conn.transaction():
            _still_running(conn, item.id)  # before uploading anything
        ReportUploader(conn, storage, origen="servidor").finish(
            data_dir / item.report_id, result.report, guard=guard
        )

    try:
        with _Heartbeat(connect, item.id, heartbeat_seconds):
            inputs = data_dir / "entradas"
            inputs.mkdir(parents=True, exist_ok=True)
            path = inputs / f"{item.report_id}.txt"
            path.write_text(request.input_text(), encoding="utf-8")
            result = run_job(
                path,
                speech_date=request.fecha,
                data_dir=data_dir,
                deps=deps_factory(),
                author=(request.autor or "").strip() or None,
                speaker_info=parse_speaker_lines(request.oradores) if request.oradores else None,
                on_step=on_step,
            )
            _with_retries(connect, store)
    except Exception as exc:  # recorded on the report and in the log, never swallowed
        logger.exception("El análisis %s falló", item.report_id)
        message = public_message(exc, secrets)

        def record(conn: psycopg.Connection) -> None:
            with conn.transaction():
                try:
                    _still_running(conn, item.id)
                except ReportError:
                    logger.warning("No se anota el error de %s: ya no es suyo", item.report_id)
                    return
                mark_error(conn, item.report_id, message)
                _finish_item(conn, item.id, "error")

        try:
            _with_retries(connect, record)
        except Exception:  # noqa: BLE001 — left en_curso; the stale check recovers it
            logger.exception("No se pudo anotar el error del análisis %s", item.report_id)
        return False
    return True


class Worker:
    """`concurrency` threads that take queued analyses until stopped. `secrets` (from
    safe_errors.secret_values) are masked in the errors the panel shows; the app must also
    call install_secret_filter after configuring logging."""

    def __init__(
        self,
        connect: Connect,
        storage: Storage,
        deps_factory: Callable[[], Deps],
        data_dir: Path,
        concurrency: int = 1,
        poll_seconds: float = POLL_SECONDS,
        *,
        secrets: Sequence[str],
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self._connect = connect
        self._storage = storage
        self._deps_factory = deps_factory
        self._data_dir = data_dir
        self._concurrency = concurrency
        self._poll = poll_seconds
        self._secrets = list(secrets)
        self._heartbeat = heartbeat_seconds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_recovery = 0.0

    def recover(self) -> int:
        self._last_recovery = time.monotonic()
        with self._connect() as conn:
            n = recover_interrupted(conn)
        if n:
            logger.warning("%d análisis interrumpidos (sin latido): se pueden reintentar", n)
        return n

    def start(self) -> None:
        self.recover()
        for i in range(self._concurrency):
            thread = threading.Thread(
                target=self._loop, args=(worker_name(i),), daemon=True, name=f"analisis-{i}"
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
        process(
            self._connect, self._storage, item, deps_factory=self._deps_factory,
            data_dir=self._data_dir, secrets=self._secrets, heartbeat_seconds=self._heartbeat,
        )  # fmt: skip
        return True

    def _loop(self, name: str) -> None:
        while not self._stop.is_set():
            try:
                if time.monotonic() - self._last_recovery > RECOVER_EVERY_SECONDS:
                    self.recover()
                busy = self.run_once(name)
            except Exception:  # database down, etc.: logged and retried after a pause
                logger.exception("El worker %s no pudo consultar la cola", name)
                busy = False
            if not busy:
                self._stop.wait(self._poll)
