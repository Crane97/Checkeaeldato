"""Cola de análisis y worker: encolar, tomar sin duplicar, ejecutar, errores y reinicios.

El análisis usa las dependencias simuladas de test_run (sin red) y la base local.
"""

import datetime as dt
from contextlib import nullcontext

import psycopg
import pytest

from pipeline.llm import LLMError
from pipeline.reports import worker as worker_module
from pipeline.reports.repository import ReportError, load_meta, load_report
from pipeline.reports.worker import (
    INTERRUPTED,
    AnalysisRequest,
    Worker,
    claim_next,
    enqueue,
    recover_interrupted,
)

from .test_knowledge_schema import db  # noqa: F401  (fixture)
from .test_reports_sync import FakeStorage
from .test_run import ARTICLE, FakeDeps

pytestmark = pytest.mark.supabase


def _request(texto: str = ARTICLE, **extra) -> AnalysisRequest:
    return AnalysisRequest(
        titulo="Artículo de prueba", tipo="entrevista", fecha=dt.date(2026, 11, 20),
        texto=texto, autor="Ana", **extra,
    )  # fmt: skip


def _queue(conn, job_id):
    return conn.execute(
        "select estado, intentos from app.analysis_queue where report_id = %s", (job_id,)
    ).fetchone()


def test_enqueue_registers_the_report_and_refuses_duplicates(db):  # noqa: F811
    job_id = enqueue(db, _request())
    assert job_id == _request().job_id()
    meta = load_meta(db, job_id)
    assert (meta.estado, meta.origen, meta.tipo) == ("en_analisis", "servidor", "entrevista")
    assert _queue(db, job_id) == ("pendiente", 0)
    with pytest.raises(ReportError, match="en cola"):
        enqueue(db, _request())


def test_items_are_taken_oldest_first_and_only_once(db):  # noqa: F811
    first = enqueue(db, _request("Primer texto."))
    second = enqueue(db, _request("Segundo texto."))
    assert claim_next(db, "w0").report_id == first
    assert claim_next(db, "w1").report_id == second
    assert claim_next(db, "w2") is None
    assert _queue(db, first) == ("en_curso", 1)


def test_a_queued_analysis_ends_as_a_draft_with_progress(db, tmp_path):  # noqa: F811
    job_id = enqueue(db, _request())
    storage = FakeStorage()
    worker = Worker(lambda: nullcontext(db), storage, FakeDeps, tmp_path / "data", secrets=[])
    assert worker.run_once("w0") is True
    meta = load_meta(db, job_id)
    assert meta.estado == "borrador"
    assert meta.progreso == {"paso": "report", "numero": 10, "total": 10}
    assert _queue(db, job_id) == ("hecho", 1)
    report = load_report(db, job_id)
    assert report.claims and f"informes/{job_id}/informe.html" in storage.files
    assert worker.run_once("w0") is False  # nothing left


def test_a_failed_analysis_is_recorded_and_the_worker_goes_on(db, tmp_path):  # noqa: F811
    class BrokenDeps(FakeDeps):
        def __getattribute__(self, name):
            if name == "llm":
                raise LLMError("Se alcanzó el límite de uso")
            return super().__getattribute__(name)

    job_id = enqueue(db, _request())
    worker = Worker(
        lambda: nullcontext(db), FakeStorage(), BrokenDeps, tmp_path / "data", secrets=[]
    )
    assert worker.run_once("w0") is True
    meta = load_meta(db, job_id)
    assert meta.estado == "error" and "límite de uso" in meta.error
    assert _queue(db, job_id) == ("error", 1)


def test_a_restart_marks_running_analyses_as_interrupted(db):  # noqa: F811
    job_id = enqueue(db, _request())
    claim_next(db, "w0")
    assert recover_interrupted(db) == 0  # it has a fresh heartbeat: another live process
    assert _queue(db, job_id) == ("en_curso", 1)
    assert recover_interrupted(db, stale_after=dt.timedelta(0)) == 1
    meta = load_meta(db, job_id)
    assert (meta.estado, meta.error) == ("error", INTERRUPTED)
    assert _queue(db, job_id) == ("interrumpido", 1)
    # It can be queued again.
    assert enqueue(db, _request()) == job_id
    assert load_meta(db, job_id).estado == "en_analisis"


def _worker(db, tmp_path, deps=FakeDeps, storage=None):  # noqa: F811
    return Worker(
        lambda: nullcontext(db), storage or FakeStorage(), deps, tmp_path / "data",
        secrets=["secreto"],
    )  # fmt: skip


def test_a_progress_update_that_fails_does_not_stop_the_analysis(db, tmp_path, monkeypatch):  # noqa: F811
    def broken(*args, **kwargs):
        raise psycopg.OperationalError("conexión cerrada por el pooler")

    monkeypatch.setattr(worker_module, "set_progress", broken)
    job_id = enqueue(db, _request())
    assert _worker(db, tmp_path).run_once("w0") is True
    assert load_meta(db, job_id).estado == "borrador"
    assert _queue(db, job_id) == ("hecho", 1)


def test_dependencies_that_cannot_be_built_are_recorded_not_left_running(db, tmp_path):  # noqa: F811
    from pipeline.config import ConfigError

    def no_deps():
        raise ConfigError("Falta TAVILY_API_KEY")

    job_id = enqueue(db, _request())
    assert _worker(db, tmp_path, deps=no_deps).run_once("w0") is True
    meta = load_meta(db, job_id)
    assert (meta.estado, meta.error) == ("error", "Falta TAVILY_API_KEY")
    assert _queue(db, job_id) == ("error", 1)


def test_unexpected_errors_reach_the_panel_without_their_details(db, tmp_path):  # noqa: F811
    def leaky():
        raise RuntimeError("connection to postgresql://u:secreto@host failed")

    job_id = enqueue(db, _request())
    _worker(db, tmp_path, deps=leaky).run_once("w0")
    error = load_meta(db, job_id).error
    assert error.startswith("Error interno (RuntimeError)") and "secreto" not in error


def test_a_storage_failure_ends_as_error_not_running(db, tmp_path):  # noqa: F811
    from pipeline.reports.files import StorageError

    class DownStorage(FakeStorage):
        def upload(self, *args, **kwargs):
            raise StorageError("Storage respondió HTTP 503", 503)

    job_id = enqueue(db, _request())
    _worker(db, tmp_path, storage=DownStorage()).run_once("w0")
    assert load_meta(db, job_id).estado == "error"
    assert _queue(db, job_id) == ("error", 1)


def test_two_simultaneous_requests_for_the_same_text_do_not_crash(db, monkeypatch):  # noqa: F811
    real_create = worker_module.create_report

    def racing_create(conn, **kwargs):
        meta = real_create(conn, **kwargs)
        # The other request inserts its queue row between our check and our insert.
        conn.execute(
            "insert into app.analysis_queue (report_id, solicitud) values (%s, '{}')",
            (kwargs["job_id"],),
        )
        return meta

    monkeypatch.setattr(worker_module, "create_report", racing_create)
    with pytest.raises(ReportError, match="a la vez"):
        enqueue(db, _request())


def test_worker_names_tell_processes_apart():
    import os

    assert worker_module.worker_name(0).endswith(f":{os.getpid()}:0")


def test_a_worker_declared_interrupted_stores_nothing(db, tmp_path):  # noqa: F811
    job_id = enqueue(db, _request())
    item = claim_next(db, "w0")
    recover_interrupted(db, stale_after=dt.timedelta(0))  # its heartbeat was missed
    storage = FakeStorage()
    ok = worker_module.process(
        lambda: nullcontext(db), storage, item, deps_factory=FakeDeps, data_dir=tmp_path / "d"
    )
    assert ok is False and storage.files == {}
    meta = load_meta(db, job_id)
    assert (meta.estado, meta.error) == ("error", INTERRUPTED)  # untouched
    assert _queue(db, job_id) == ("interrumpido", 1)


def test_the_cli_cannot_register_a_text_queued_on_the_server(db):  # noqa: F811
    from pipeline.reports.repository import create_report

    job_id = enqueue(db, _request())
    with pytest.raises(ReportError, match="cola"):
        create_report(
            db, job_id=job_id, titulo="x", fecha=dt.date(2026, 11, 20), input_kind="texto",
            fuente="x.txt", origen="local",
        )  # fmt: skip
