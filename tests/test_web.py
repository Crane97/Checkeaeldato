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


@pytest.fixture
def client(tmp_path: Path, deps: FakeDeps) -> TestClient:
    app = create_app(data_dir=tmp_path / "data", deps_factory=lambda: deps, run_in_thread=False)
    return TestClient(app)


def start(client: TestClient, **overrides) -> dict:
    body = {"texto": ARTICLE, "fecha": DATE, "autor": "Ana Oradora", "recalcular": False}
    response = client.post("/api/analisis", json=body | overrides)
    assert response.status_code == 202, response.text
    return response.json()


def test_home_page_has_the_form(client):
    response = client.get("/")
    assert response.status_code == 200
    for element in ('id="texto"', 'id="fecha"', 'id="autor"', 'id="recalcular"'):
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
        "claims",
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
    html = (STATIC_DIR / "index.html").read_text()
    assert 'id="oradores"' in html and "by_speaker" in html
