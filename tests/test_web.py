from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.neutrality import FORBIDDEN_WORDS_RE
from pipeline.web.app import MAX_TEXT_CHARS, STATIC_DIR, create_app
from tests.test_run import ARTICLE, FakeDeps

DATE = "2026-11-20"


@pytest.fixture
def deps() -> FakeDeps:
    return FakeDeps()


class FakeSender:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.error = error

    def __call__(self, report, to: str) -> None:
        if self.error:
            raise self.error
        self.sent.append((report.job_id, to))


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def client(tmp_path: Path, deps: FakeDeps, sender: FakeSender) -> TestClient:
    app = create_app(
        data_dir=tmp_path / "data",
        deps_factory=lambda: deps,
        run_in_thread=False,
        email_sender=sender,
    )
    return TestClient(app)


def start(client: TestClient, **overrides) -> dict:
    body = {"texto": ARTICLE, "fecha": DATE, "autor": "Ana Oradora", "recalcular": False}
    response = client.post("/api/analisis", json=body | overrides)
    assert response.status_code == 202, response.text
    return response.json()


def test_home_page_has_the_form(client):
    response = client.get("/")
    assert response.status_code == 200
    for element in ('id="texto"', 'id="fecha"', 'id="autor"', 'id="email"', 'id="recalcular"'):
        assert element in response.text


def test_page_uses_neutral_wording():
    for path in STATIC_DIR.glob("*"):
        assert not FORBIDDEN_WORDS_RE.search(path.read_text()), path.name


def test_analysis_runs_and_returns_the_report(client, deps):
    job = start(client)

    status = client.get(f"/api/analisis/{job['job_id']}").json()

    assert status["estado"] == "terminado"
    assert status["error"] is None
    report = status["informe"]
    assert report["speakers"] == {"autor": "Ana Oradora"}
    [claim] = report["claims"]
    assert claim["category"] == "falso_con_fuente_oficial"
    assert claim["sources"][0]["url"] == "https://www.ine.es/epa"
    assert [p["paso"] for p in status["pasos"]] == [
        "transcript",
        "speakers",
        "speaker_research",
        "claims",
        "context",
        "evidence",
        "verdicts",
        "report",
    ]
    assert all(p["estado"] == "done" for p in status["pasos"])
    assert deps.llm.calls == ["claims", "verdicts"]


def test_same_text_reuses_results_unless_recalculating(client, deps):
    start(client)
    deps.llm.calls.clear()

    start(client)
    assert deps.llm.calls == []

    start(client, recalcular=True)
    assert deps.llm.calls == ["claims", "verdicts"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"texto": "   "}, "vacío"),
        ({"texto": "x" * (MAX_TEXT_CHARS + 1)}, "demasiado largo"),
        ({"fecha": "20/11/2026"}, "fecha"),
    ],
)
def test_invalid_requests_are_rejected(client, overrides, message):
    body = {"texto": ARTICLE, "fecha": DATE} | overrides
    response = client.post("/api/analisis", json=body)
    assert response.status_code in (400, 422)
    assert message in response.text


def test_pipeline_errors_are_reported_in_the_status(client, deps):
    def broken(**kwargs):
        from pipeline.llm import LLMConfigError

        raise LLMConfigError("No hay credenciales de Anthropic")

    deps.llm.structured = broken
    job = start(client)
    status = client.get(f"/api/analisis/{job['job_id']}").json()
    assert status["estado"] == "error"
    assert "credenciales" in status["error"]
    assert status["informe"] is None


def test_unknown_job_is_404(client):
    assert client.get("/api/analisis/000000000000").status_code == 404


def test_only_one_analysis_at_a_time(tmp_path, deps):
    app = create_app(data_dir=tmp_path / "data", deps_factory=lambda: deps, run_in_thread=False)
    app.state.jobs.busy_with = "otro"
    response = TestClient(app).post("/api/analisis", json={"texto": ARTICLE, "fecha": DATE})
    assert response.status_code == 409


def test_page_has_per_speaker_table():
    assert 'id="oradores"' in (STATIC_DIR / "index.html").read_text()
    assert "by_speaker" in (STATIC_DIR / "report.js").read_text()


def test_report_is_emailed_when_an_address_is_given(client, sender):
    job = start(client, email="ana@example.com")
    status = client.get(f"/api/analisis/{job['job_id']}").json()
    assert sender.sent == [(job["job_id"], "ana@example.com")]
    assert status["correo"] == {"estado": "enviado", "destino": "ana@example.com", "detalle": None}


def test_no_email_without_an_address(client, sender):
    job = start(client)
    assert sender.sent == []
    assert client.get(f"/api/analisis/{job['job_id']}").json()["correo"] is None


def test_email_failure_is_shown_but_results_stay_available(tmp_path, deps):
    from pipeline.email_report import EmailError

    app = create_app(
        data_dir=tmp_path / "data",
        deps_factory=lambda: deps,
        run_in_thread=False,
        email_sender=FakeSender(EmailError("El servidor rechazó la contraseña")),
    )
    client = TestClient(app)
    job = start(client, email="ana@example.com")
    status = client.get(f"/api/analisis/{job['job_id']}").json()
    assert status["estado"] == "terminado"
    assert status["informe"] is not None
    assert status["correo"]["estado"] == "error"
    assert "contraseña" in status["correo"]["detalle"]


def test_invalid_email_is_rejected(client):
    response = client.post(
        "/api/analisis", json={"texto": ARTICLE, "fecha": DATE, "email": "no-es-un-correo"}
    )
    assert response.status_code == 400
    assert "correo" in response.text


def test_interactive_report_can_be_downloaded(client):
    job = start(client)
    response = client.get(f"/api/analisis/{job['job_id']}/informe.html")
    assert response.status_code == 200
    assert "function renderReport" in response.text
    assert "attachment" in response.headers["content-disposition"]


def test_shared_assets_are_served(client):
    assert "renderReport" in client.get("/static/report.js").text
    assert "--confusion" in client.get("/static/report.css").text


def test_speaker_lines_from_the_form_reach_the_report(client):
    text = "Ana Ruiz: Hola a todos.\nLuis Gómez: El paro ha bajado un 20 % este año.\n"
    job = start(client, texto=text, oradores="Luis Gómez | PP | candidato")
    report = client.get(f"/api/analisis/{job['job_id']}").json()["informe"]
    assert report["speakers"]["Luis Gómez"] == "Luis Gómez (PP)"
