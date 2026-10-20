"""Subir un análisis a Supabase: archivos de auditoría, resultado como borrador y errores."""

import datetime as dt
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pipeline import cli
from pipeline.reports.files import StoredFile
from pipeline.reports.repository import load_meta, load_report
from pipeline.reports.sync import AUDIT_FILES, ReportUploader, job_id_for

from .test_knowledge_schema import db  # noqa: F401  (fixture)
from .test_reports_repository import JOB, claim, report
from .test_run import ARTICLE, FakeDeps


class FakeStorage:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.buckets: list[tuple[str, bool]] = []

    def ensure_bucket(self, name: str, *, public: bool) -> None:
        self.buckets.append((name, public))

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> StoredFile:
        self.files[f"{bucket}/{path}"] = data
        return StoredFile(ruta=f"{bucket}/{path}", sha256="a" * 64, bytes=len(data))


def test_the_job_id_is_the_same_as_the_job_folder(tmp_path):
    path = tmp_path / "x.txt"
    path.write_text("hola")
    assert job_id_for(path) == "b221d9dbb083"  # sha256("hola")[:12]


@pytest.mark.supabase
def test_a_finished_analysis_is_stored_as_a_draft_with_its_audit_files(db, tmp_path):  # noqa: F811
    job_dir = tmp_path / JOB
    job_dir.mkdir()
    for name in [*AUDIT_FILES, "audio.wav", "input.mp4", "audio.json"]:
        (job_dir / name).write_text(json.dumps({"archivo": name}))
    storage = FakeStorage()
    uploader = ReportUploader(db, storage, origen="local")
    meta = uploader.begin(job_id=JOB, titulo="Debate a tres", fecha=dt.date(2026, 10, 18),
                          input_kind="texto", fuente="debate.txt", tipo="debate")  # fmt: skip
    assert meta.estado == "en_analisis"

    meta = uploader.finish(job_dir, report(claim("c1", "A")))
    assert meta.estado == "borrador"
    assert storage.buckets == [("informes", False)]
    assert set(storage.files) == {f"informes/{JOB}/{n}" for n in AUDIT_FILES} | {
        f"informes/{JOB}/informe.html"
    }  # never the video or the audio
    assert b"function renderReport" in storage.files[f"informes/{JOB}/informe.html"]
    tipos = {
        t for (t,) in db.execute("select tipo from app.report_files where report_id = %s", (JOB,))
    }
    assert tipos == set(AUDIT_FILES.values()) | {"informe_html"}
    assert [c.id for c in load_report(db, JOB).claims] == ["c1"]


@pytest.mark.supabase
def test_a_failed_analysis_is_recorded_with_its_message(db):  # noqa: F811
    uploader = ReportUploader(db, FakeStorage(), origen="local")
    uploader.begin(job_id=JOB, titulo="Debate", fecha=dt.date(2026, 10, 18),
                   input_kind="texto", fuente="debate.txt")  # fmt: skip
    uploader.fail(JOB, "Se alcanzó el límite de uso")
    meta = load_meta(db, JOB)
    assert (meta.estado, meta.error) == ("error", "Se alcanzó el límite de uso")


# ── CLI: --subir ─────────────────────────────────────────────────────────────


class FakeUploader:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def begin(self, **kwargs):
        self.calls.append(("begin", kwargs))

    def finish(self, job_dir: Path, report):
        self.calls.append(("finish", job_dir.name, len(report.claims)))
        return type("Meta", (), {"slug": "2026-11-20-articulo", "estado": "borrador"})()

    def fail(self, job_id: str, message: str):
        self.calls.append(("fail", job_id, message))

    def close(self):
        self.calls.append(("close",))


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    from pipeline.config import Settings

    uploader = FakeUploader()
    monkeypatch.setattr(cli, "LiveDeps", lambda settings, **kwargs: FakeDeps())
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, data_dir=tmp_path / "data")
    )
    monkeypatch.setattr(cli, "open_uploader", lambda settings: uploader)
    article = tmp_path / "articulo.txt"
    article.write_text(ARTICLE)
    return uploader, article


def test_subir_registers_the_analysis_and_uploads_it(cli_env):
    uploader, article = cli_env
    result = CliRunner().invoke(
        cli.app,
        [str(article), "--fecha", "2026-11-20", "--subir", "--titulo", "Artículo",
         "--tipo", "entrevista"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    begin, finish, close = uploader.calls
    assert begin[0] == "begin" and begin[1]["titulo"] == "Artículo"
    assert begin[1]["tipo"] == "entrevista" and begin[1]["job_id"] == job_id_for(article)
    assert finish == ("finish", job_id_for(article), 1)
    assert close == ("close",)
    assert "borrador" in result.output and "2026-11-20-articulo" in result.output


def test_subir_records_a_failed_analysis(cli_env, monkeypatch):
    uploader, article = cli_env
    from pipeline.llm import LLMError

    def boom(*args, **kwargs):
        raise LLMError("Se alcanzó el límite de uso")

    monkeypatch.setattr(cli, "run_job", boom)
    result = CliRunner().invoke(cli.app, [str(article), "--fecha", "2026-11-20", "--subir"])
    assert result.exit_code == 1
    assert ("fail", job_id_for(article), "Se alcanzó el límite de uso") in uploader.calls
    assert uploader.calls[0][1]["titulo"] == "articulo"  # file name when no title is given


def test_title_and_type_only_make_sense_with_subir(cli_env):
    _, article = cli_env
    result = CliRunner().invoke(
        cli.app, [str(article), "--fecha", "2026-11-20", "--titulo", "Artículo"]
    )
    assert result.exit_code == 2 and "--subir" in result.output
    result = CliRunner().invoke(
        cli.app, [str(article), "--fecha", "2026-11-20", "--subir", "--tipo", "charla"]
    )
    assert result.exit_code == 2 and "debate" in result.output


def test_subir_records_an_interrupted_analysis(cli_env, monkeypatch):
    uploader, article = cli_env

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_job", interrupted)
    CliRunner().invoke(cli.app, [str(article), "--fecha", "2026-11-20", "--subir"])
    assert ("fail", job_id_for(article), "Análisis interrumpido: KeyboardInterrupt") in (
        uploader.calls
    )
