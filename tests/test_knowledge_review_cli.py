"""Flujo interactivo de `conocimiento revisar` (sin base de datos)."""

from typer.testing import CliRunner

from pipeline.knowledge import cli
from pipeline.knowledge.review import ReviewItem


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass

    def rollback(self):
        pass


def _run(monkeypatch, keys: str, items: list[ReviewItem]):
    decided = []
    monkeypatch.setattr(cli, "_connect", FakeConn)
    monkeypatch.setattr(cli, "pending_items", lambda conn, kinds, indicator=None: items)
    monkeypatch.setattr(
        cli,
        "decide",
        lambda conn, kind, item_id, outcome, reviewer: decided.append((item_id, outcome, reviewer)),
    )
    result = CliRunner().invoke(cli.app, ["revisar", "--revisor", "Jorge"], input=keys)
    assert result.exit_code == 0, result.output
    return decided, result.output


def item(i: str, group: str = "observacion:smi_mensual") -> ReviewItem:
    return ReviewItem("observacion", i, f"SMI {i}", group, [f"Valor: {i}"])


def test_each_item_is_approved_rejected_or_skipped(monkeypatch):
    decided, out = _run(monkeypatch, "a\nr\ns\n", [item("1"), item("2"), item("3")])
    assert decided == [("1", "aprobado", "Jorge"), ("2", "rechazado", "Jorge")]
    assert "Aprobados: 1 · rechazados: 1" in out


def test_bulk_approval_stays_within_the_group_and_needs_confirmation(monkeypatch):
    items = [item("1"), item("2"), item("3", group="gobierno")]
    decided, _ = _run(monkeypatch, "t\ny\nq\n", items)
    assert decided == [("1", "aprobado", "Jorge"), ("2", "aprobado", "Jorge")]
    decided, _ = _run(monkeypatch, "t\nn\nq\n", items)
    assert decided == []


def test_ingestion_alerts_are_emailed_when_something_needs_attention(monkeypatch):
    from pipeline.config import Settings
    from pipeline.knowledge.ingest import IndicatorResult, IngestReport

    report = IngestReport(
        results=[IndicatorResult("paro_epa", "error", error="INE no responde")],
        stale=["ipc_interanual: último dato del 2026-01-31"],
    )
    sent = []
    monkeypatch.setattr(cli, "_connect", FakeConn)
    monkeypatch.setattr(cli, "ingest", lambda *a, **k: report)
    monkeypatch.setattr(cli, "load_catalog", lambda: {})
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(alert_email_to="alertas@example.org", smtp_user="x")
    )
    monkeypatch.setattr("pipeline.email_report.send_email", lambda msg, s: sent.append(msg))
    result = CliRunner().invoke(cli.app, ["ingerir", "--avisar"])
    assert result.exit_code == 2  # errors fail the run (and the GitHub Action)
    [message] = sent
    assert message["To"] == "alertas@example.org"
    body = message.get_content()
    assert "INE no responde" in body and "ipc_interanual" in body
