"""Cola de análisis y worker: encolar, tomar sin duplicar, ejecutar, errores y reinicios.

El análisis usa las dependencias simuladas de test_run (sin red) y la base local.
"""

import datetime as dt
from contextlib import nullcontext

import pytest

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
    worker = Worker(lambda: nullcontext(db), storage, FakeDeps, tmp_path / "data")
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
                raise RuntimeError("Se alcanzó el límite de uso")
            return super().__getattribute__(name)

    job_id = enqueue(db, _request())
    worker = Worker(lambda: nullcontext(db), FakeStorage(), BrokenDeps, tmp_path / "data")
    assert worker.run_once("w0") is True
    meta = load_meta(db, job_id)
    assert meta.estado == "error" and "límite de uso" in meta.error
    assert _queue(db, job_id) == ("error", 1)


def test_a_restart_marks_running_analyses_as_interrupted(db):  # noqa: F811
    job_id = enqueue(db, _request())
    claim_next(db, "w0")
    assert recover_interrupted(db) == 1
    meta = load_meta(db, job_id)
    assert (meta.estado, meta.error) == ("error", INTERRUPTED)
    assert _queue(db, job_id) == ("interrumpido", 1)
    # It can be queued again.
    assert enqueue(db, _request()) == job_id
    assert load_meta(db, job_id).estado == "en_analisis"
