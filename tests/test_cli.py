from pathlib import Path

import pytest
from typer.testing import CliRunner

from pipeline import cli
from tests.test_run import ARTICLE, FakeDeps

runner = CliRunner()


@pytest.fixture
def article(tmp_path: Path) -> Path:
    path = tmp_path / "articulo.txt"
    path.write_text(ARTICLE)
    return path


@pytest.fixture
def fake_deps(monkeypatch, tmp_path):
    from pipeline.config import Settings

    monkeypatch.setattr(cli, "LiveDeps", lambda settings: FakeDeps())
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, data_dir=tmp_path / "data")
    )


def test_text_run_prints_summary(article, fake_deps):
    result = runner.invoke(cli.app, [str(article), "--fecha", "2026-11-20", "--autor", "Ana"])
    assert result.exit_code == 0, result.output
    assert "Afirmaciones verificables: 1" in result.output
    assert "Ana" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--fecha", "20/11/2026"], "AAAA-MM-DD"),
        (["--fecha", "2026-11-20", "--oradores", "speaker_0"], "speaker_0=Nombre"),
        (["--fecha", "2026-11-20", "--from-step", "todo"], "paso desconocido"),
    ],
)
def test_bad_arguments_are_explained(article, fake_deps, args, message):
    result = runner.invoke(cli.app, [str(article), *args])
    assert result.exit_code == 2
    assert message in result.output


def test_date_is_required(article, fake_deps):
    result = runner.invoke(cli.app, [str(article)])
    assert result.exit_code == 2
    assert "--fecha" in result.output


def test_user_errors_are_shown_without_traceback(tmp_path, fake_deps):
    result = runner.invoke(cli.app, [str(tmp_path / "no_existe.txt"), "--fecha", "2026-11-20"])
    assert result.exit_code == 1
    assert "No existe el archivo" in result.output
    assert "repite el mismo comando" in result.output
    assert "Traceback" not in result.output


def test_email_option_sends_the_report(article, fake_deps, monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "email_report", lambda report, to, settings: sent.append(to))
    result = runner.invoke(
        cli.app, [str(article), "--fecha", "2026-11-20", "--email", "ana@example.com"]
    )
    assert result.exit_code == 0, result.output
    assert sent == ["ana@example.com"]
    assert "Informe enviado a ana@example.com" in result.output


def test_email_failure_keeps_the_summary_and_explains(article, fake_deps, monkeypatch):
    from pipeline.email_report import EmailError

    def broken(report, to, settings):
        raise EmailError("contraseña rechazada")

    monkeypatch.setattr(cli, "email_report", broken)
    result = runner.invoke(
        cli.app, [str(article), "--fecha", "2026-11-20", "--email", "ana@example.com"]
    )
    assert result.exit_code == 1
    assert "Afirmaciones verificables" in result.output
    assert "contraseña rechazada" in result.output


def test_invalid_email_option_is_rejected(article, fake_deps):
    result = runner.invoke(cli.app, [str(article), "--fecha", "2026-11-20", "--email", "x"])
    assert result.exit_code == 2


def test_speakers_file_is_parsed_and_bad_files_explained(article, fake_deps, tmp_path):
    good = tmp_path / "speakers.json"
    good.write_text('{"autor": {"name": "Ana Ruiz", "party": "PSOE"}}')
    result = runner.invoke(
        cli.app, [str(article), "--fecha", "2026-11-20", "--speakers", str(good)]
    )
    assert result.exit_code == 0, result.output

    bad = tmp_path / "bad.json"
    bad.write_text("no es json")
    result = runner.invoke(cli.app, [str(article), "--fecha", "2026-11-20", "--speakers", str(bad)])
    assert result.exit_code == 2
    assert "SPEAKER_0" in result.output
