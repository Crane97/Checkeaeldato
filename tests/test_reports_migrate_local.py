"""Migración única de data/ a Supabase: informes terminados y caché de búsquedas."""

import datetime as dt
import json

import pytest

from pipeline.evidence.search_cache import SearchCache, cache_key
from pipeline.models import JobManifest, StepRecord
from pipeline.reports.migrate_local import (
    MigrationReport,
    migrate_reports,
    migrate_search_cache,
    title_from_source,
)
from pipeline.reports.repository import load_meta
from pipeline.reports.sync import ReportUploader

from .test_knowledge_schema import db  # noqa: F401  (fixture)
from .test_reports_repository import JOB, claim, report
from .test_reports_sync import FakeStorage
from .test_search_cache_db import RESPONSE, MemoryStore

NOW = dt.datetime(2026, 10, 20, tzinfo=dt.UTC)


def _job(data_dir, job_id, *, finished=True):
    job_dir = data_dir / job_id
    job_dir.mkdir(parents=True)
    steps = {"report": StepRecord(status="done", finished_at=NOW)} if finished else {}
    manifest = JobManifest(
        job_id=job_id, input_path="x.txt", input_sha256="0" * 64, input_kind="texto",
        speech_date=dt.date(2026, 10, 18), created_at=NOW, steps=steps,
    )  # fmt: skip
    (job_dir / "manifest.json").write_text(manifest.model_dump_json())
    if finished:
        r = report(claim("c1", "A")).model_copy(update={"job_id": job_id})
        (job_dir / "report.json").write_text(r.model_dump_json())


@pytest.mark.parametrize(
    ("source", "title"),
    [("debate_a_tres-2026.txt", "Debate a tres 2026"), ("x.mp4", "X"), ("___.txt", "Informe")],
)
def test_titles_come_from_the_file_name(source, title):
    assert title_from_source(source) == title


@pytest.mark.supabase
def test_finished_reports_are_uploaded_once_as_drafts(db, tmp_path):  # noqa: F811
    data = tmp_path / "data"
    _job(data, JOB)
    _job(data, "fedcba987654", finished=False)
    (data / "cache").mkdir()  # not a job
    uploader = ReportUploader(db, FakeStorage(), origen="local")

    simulated = migrate_reports(db, uploader, data, simulate=True)
    assert simulated.uploaded == [JOB]
    assert db.execute("select count(*) from app.reports").fetchone() == (0,)

    result = migrate_reports(db, uploader, data)
    assert (result.uploaded, result.unfinished) == ([JOB], ["fedcba987654"])
    meta = load_meta(db, JOB)
    assert (meta.estado, meta.titulo, meta.tipo) == ("borrador", "Debate", None)
    assert migrate_reports(db, uploader, data).already == [JOB]


def test_cache_entries_are_copied_with_their_date_and_only_once(tmp_path):
    request = {"query": "paro EPA 2026", "search_depth": "basic", "max_results": 5}
    saved = NOW - dt.timedelta(days=5)
    SearchCache(tmp_path / "search", now=lambda: saved).put(request, RESPONSE)
    (tmp_path / "search" / "ab").mkdir()
    (tmp_path / "search" / "ab" / ("a" * 64 + ".json")).write_text("{roto")  # skipped
    store = MemoryStore()

    result = MigrationReport()
    migrate_search_cache(store, tmp_path / "search", result)
    assert result.cache_copied == 1
    assert store.rows[cache_key(request)] == (RESPONSE, saved)

    again = MigrationReport()
    migrate_search_cache(store, tmp_path / "search", again)
    assert (again.cache_copied, again.cache_present) == (0, 1)


def test_simulated_cache_migration_writes_nothing(tmp_path):
    request = {"query": "IPC 2025", "search_depth": "basic", "max_results": 5}
    SearchCache(tmp_path / "search", now=lambda: NOW).put(request, {"results": []})
    store = MemoryStore()
    result = MigrationReport()
    migrate_search_cache(store, tmp_path / "search", result, simulate=True)
    assert result.cache_copied == 1 and store.rows == {}
    assert json.loads(next((tmp_path / "search").glob("*/*.json")).read_text())["response"] == {
        "results": []
    }


def test_a_database_failure_stops_the_cache_copy_instead_of_counting_it(tmp_path):
    import psycopg

    from pipeline.evidence.search_cache import DbSearchStore

    request = {"query": "IPC 2025", "search_depth": "basic", "max_results": 5}
    SearchCache(tmp_path / "search", now=lambda: NOW).put(request, {"results": []})

    def broken():
        raise psycopg.OperationalError("sin conexión")

    result = MigrationReport()
    with pytest.raises(psycopg.OperationalError):
        migrate_search_cache(DbSearchStore(broken, strict=True), tmp_path / "search", result)
    assert result.cache_copied == 0


@pytest.mark.supabase
def test_a_half_failed_upload_is_retried_on_the_next_run(db, tmp_path):  # noqa: F811
    data = tmp_path / "data"
    _job(data, JOB)

    class FailingStorage(FakeStorage):
        def upload(self, *args, **kwargs):
            raise RuntimeError("Storage caído")

    result = migrate_reports(db, ReportUploader(db, FailingStorage(), origen="local"), data)
    assert result.failed and not result.uploaded
    assert load_meta(db, JOB).estado == "error"  # not left "en análisis"

    again = migrate_reports(db, ReportUploader(db, FakeStorage(), origen="local"), data)
    assert (again.uploaded, again.retried, again.already) == ([JOB], [JOB], [])
    assert load_meta(db, JOB).estado == "borrador"


def test_an_unreadable_folder_is_a_failure_not_an_unfinished_analysis(tmp_path):
    from pipeline.reports.migrate_local import finished_jobs

    job_dir = tmp_path / "data" / "abcdef012345"
    job_dir.mkdir(parents=True)
    (job_dir / "manifest.json").write_text("{roto")
    [(found_dir, report_, error)] = finished_jobs(tmp_path / "data")
    assert found_dir == job_dir and report_ is None and "ilegible" in error
