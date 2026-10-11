import datetime as dt
import json
import re

import pytest

from pipeline.config import ConfigError, Settings
from pipeline.email_report import build_email, is_valid_email, send_email
from pipeline.models import Claim, Report, RunCost, Source
from pipeline.neutrality import FORBIDDEN_WORDS_RE
from pipeline.report_html import render_report_html

INE = Source(url="https://www.ine.es/epa", title="EPA", organization="INE", kind="oficial")
EVIL = '</script><script>alert("x")</script> & <b>negrita</b>'


def claim(cid: str, speaker: str, verdict: str, quote: str = "El paro bajó un 15 %") -> Claim:
    return Claim(
        id=cid,
        speaker=speaker,
        text_original=quote,
        text_normalized=f"{quote}.",
        claim_type="estadistica",
        verdict=verdict,
        confidence=0.0 if verdict == "no_verificable" else 0.9,
        explanation="Según la EPA, el paro bajó un 14,6 %.",
        sources=[] if verdict == "no_verificable" else [INE],
    )


@pytest.fixture
def report() -> Report:
    return Report(
        job_id="abc123def456",
        source="debate.txt",
        input_kind="texto",
        speech_date=dt.date(2026, 11, 20),
        generated_at=dt.datetime(2026, 11, 20, 22, 30, tzinfo=dt.UTC),
        speakers={"autor": "Moderación"},
        claims=[
            claim("c001", "Candidata A", "verdadero"),
            claim("c002", "Candidato B", "falso", quote=EVIL),
            claim("c003", "Candidata A", "no_verificable", quote="Dato sin fuente 7 %"),
        ],
        cost=RunCost(),
    )


# --- interactive HTML report ------------------------------------------------


def test_html_report_is_self_contained_and_embeds_the_data(report):
    html = render_report_html(report)
    assert "function renderReport" in html and "--falso_con_fuente_oficial" in html
    assert 'src="http' not in html and 'href="/static' not in html  # no external files
    data = re.search(r"const REPORT = (.*?);</script>", html, re.S).group(1)
    parsed = json.loads(data)
    assert parsed["by_speaker"]["Candidata A"]["verdadero"] == 1
    assert parsed["claims"][1]["text_original"] == EVIL


def test_html_report_cannot_be_broken_by_external_text(report):
    html = render_report_html(report)
    assert '<script>alert("x")' not in html
    assert html.count("</script>") == 3  # data, renderer and the render call


def test_html_report_wording_is_neutral(report):
    assert not FORBIDDEN_WORDS_RE.search(render_report_html(report))


# --- email ------------------------------------------------------------------


def test_email_has_summary_body_and_interactive_attachment(report):
    message = build_email(report, to="ana@example.com", sender="verificador@example.com")

    assert message["To"] == "ana@example.com"
    assert message["From"] == "verificador@example.com"
    assert "3 afirmaciones" in message["Subject"] and "debate.txt" in message["Subject"]

    text = message.get_body(("plain",)).get_content()
    assert "Candidata A" in text and "Falso con fuente oficial" in text

    html = message.get_body(("html",)).get_content()
    assert "Candidato B" in html and "Por orador" in html
    assert "<script" not in html.lower()  # email clients block scripts: static body
    assert "&lt;/script&gt;" in html  # external text is escaped
    assert "informe-abc123def456.html" in html  # tells the reader to open the attachment

    [attachment] = list(message.iter_attachments())
    assert attachment.get_filename() == "informe-abc123def456.html"
    assert "function renderReport" in attachment.get_content()


@pytest.mark.parametrize(
    ("address", "valid"),
    [
        ("ana@example.com", True),
        ("ana.perez+x@sub.example.es", True),
        ("ana", False),
        ("ana@", False),
        ("a b@example.com", False),
        ("ana@example.com\nBcc: x@y.z", False),
    ],
)
def test_email_address_validation(address, valid):
    assert is_valid_email(address) is valid


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls: list[str] = []
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login {user} {password}")

    def send_message(self, message):
        self.sent = message


def settings(**overrides) -> Settings:
    data = {
        "smtp_host": "smtp.example.com",
        "smtp_user": "verificador@example.com",
        "smtp_password": "app-password",
    }
    return Settings(_env_file=None, **data | overrides)


def test_send_uses_starttls_on_587(monkeypatch, report):
    import pipeline.email_report as module

    FakeSMTP.instances.clear()
    monkeypatch.setattr(module.smtplib, "SMTP", FakeSMTP)
    message = build_email(report, to="ana@example.com", sender="verificador@example.com")
    send_email(message, settings())
    [smtp] = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.calls == ["starttls", "login verificador@example.com app-password"]
    assert smtp.sent is message


def test_send_uses_ssl_on_465(monkeypatch, report):
    import pipeline.email_report as module

    FakeSMTP.instances.clear()
    monkeypatch.setattr(module.smtplib, "SMTP_SSL", FakeSMTP)
    message = build_email(report, to="ana@example.com", sender="verificador@example.com")
    send_email(message, settings(smtp_port=465))
    assert FakeSMTP.instances[0].calls == ["login verificador@example.com app-password"]


def test_missing_smtp_settings_explain_what_to_configure(report):
    message = build_email(report, to="ana@example.com", sender="x@example.com")
    with pytest.raises(ConfigError, match="SMTP_HOST"):
        send_email(message, Settings(_env_file=None))
