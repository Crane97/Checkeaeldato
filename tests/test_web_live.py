import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.live.session import LiveEvent
from pipeline.web.app import create_app
from pipeline.web.live import LiveManager, LiveRequest, sse, validate_source
from tests.test_live_session import FakeBlockSource, make_blocks
from tests.test_run import DATE, FakeDeps

NOW = dt.datetime(2026, 11, 20, 21, 0, tzinfo=dt.UTC)
URL = "https://emisora.example/directo.m3u8"


@pytest.fixture
def deps() -> FakeDeps:
    return FakeDeps()


@pytest.fixture
def factory(tmp_path):
    """Block sources that yield two fake blocks; remembers what it was asked for."""
    calls: list[tuple[str, Path, int]] = []

    def make(source: str, out_dir: Path, block_seconds: int):
        calls.append((source, out_dir, block_seconds))
        return FakeBlockSource(make_blocks(tmp_path, 2))

    make.calls = calls  # type: ignore[attr-defined]
    return make


@pytest.fixture
def client(tmp_path, deps, factory) -> TestClient:
    app = create_app(
        data_dir=tmp_path / "data",
        deps_factory=lambda: deps,
        run_in_thread=False,
        source_factory=factory,
    )
    return TestClient(app)


def start(client: TestClient, **overrides) -> dict:
    body = {"fuente": URL, "fecha": DATE.isoformat()} | overrides
    response = client.post("/api/directo", json=body)
    assert response.status_code == 202, response.text
    return response.json()


def test_live_page_is_served(client):
    response = client.get("/directo")
    assert response.status_code == 200
    assert 'id="fuente"' in response.text and "EventSource" in response.text


def test_a_session_runs_and_its_snapshot_has_the_running_report(client, factory):
    session = start(client, bloque_segundos=90)

    snapshot = client.get(f"/api/directo/{session['session_id']}").json()

    assert snapshot["estado"] == "terminado" and snapshot["error"] is None
    assert snapshot["bloque_segundos"] == 90
    assert [b["status"] for b in snapshot["bloques"]] == ["hecho", "hecho"]
    assert [c["id"] for c in snapshot["informe"]["claims"]] == ["b000c001", "b001c001"]
    assert snapshot["informe"]["by_category"]["falso_con_fuente_oficial"] == 2
    assert snapshot["pendientes"] == []
    assert snapshot["etiquetas"] == ["speaker_0@0", "speaker_1@0", "speaker_0@1", "speaker_1@1"]
    assert len(snapshot["transcripcion"]) == 4
    [(source, out_dir, seconds)] = factory.calls
    assert source == URL and seconds == 90 and out_dir.name == "blocks"


def test_events_are_streamed_until_the_session_ends(client):
    session = start(client)

    with client.stream("GET", f"/api/directo/{session['session_id']}/eventos") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events = [chunk for chunk in body.split("\n\n") if chunk.startswith("id:")]
    types = [line[7:] for e in events for line in e.split("\n") if line.startswith("event: ")]
    assert types[0] == "bloque" and types[-1] == "estado"
    assert types.count("veredicto") == 2
    last = json.loads(events[-1].split("\ndata: ")[1])
    assert last == {"estado": "terminado", "error": None}


def test_events_can_be_resumed_from_a_sequence_number(client):
    session = start(client)
    snapshot = client.get(f"/api/directo/{session['session_id']}").json()
    after = snapshot["ultimo_evento"] - 1

    with client.stream("GET", f"/api/directo/{session['session_id']}/eventos?desde={after}") as r:
        body = "".join(r.iter_text())
    assert body.count("\nevent: ") == 1 and f"id: {after + 1}\n" in body


def test_speakers_can_be_named_while_the_session_runs(client):
    session = start(client)
    response = client.put(
        f"/api/directo/{session['session_id']}/oradores/speaker_1@0",
        json={"name": "Ana Ruiz", "party": "PSOE", "role": "candidata"},
    )
    assert response.status_code == 200
    assert response.json()["speaker_1@0"] == "Ana Ruiz (PSOE)"
    snapshot = client.get(f"/api/directo/{session['session_id']}").json()
    assert snapshot["informe"]["speakers"]["speaker_1@0"] == "Ana Ruiz (PSOE)"


def test_speaker_lines_from_the_form_reach_the_session(client):
    session = start(client, oradores="speaker_1@0 | PP | candidato")
    snapshot = client.get(f"/api/directo/{session['session_id']}").json()
    assert snapshot["oradores"]["speaker_1@0"].endswith("(PP)")


def test_only_one_live_session_at_a_time(tmp_path, deps, factory):
    app = create_app(data_dir=tmp_path / "data", deps_factory=lambda: deps, source_factory=factory)
    manager = app.state.live
    first = manager.start(LiveRequest(fuente=URL, fecha=DATE.isoformat()))
    manager.runners[first].state.status = "en_curso"
    response = TestClient(app).post("/api/directo", json={"fuente": URL, "fecha": "2026-11-20"})
    assert response.status_code == 409


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"fuente": "   "}, "URL"),
        ({"fuente": "ftp://x/y"}, "Solo se admiten"),
        ({"fuente": "no-existe.mp4"}, "No existe"),
        ({"fecha": "20/11/2026"}, "fecha"),
        ({"bloque_segundos": 5}, "bloque_segundos"),
    ],
)
def test_invalid_requests_are_rejected(client, overrides, message):
    body = {"fuente": URL, "fecha": DATE.isoformat()} | overrides
    response = client.post("/api/directo", json=body)
    assert response.status_code in (400, 422), response.text
    assert message in response.text


def test_a_recorded_file_is_accepted(tmp_path, client):
    video = tmp_path / "debate.mp4"
    video.write_bytes(b"")
    assert validate_source(str(video)) == str(video)


def test_unknown_session_is_404(client):
    assert client.get("/api/directo/000000000000").status_code == 404
    assert client.post("/api/directo/000000000000/parar").status_code == 404
    with client.stream("GET", "/api/directo/000000000000/eventos") as response:
        assert response.status_code == 404


def test_sessions_are_listed_newest_first(tmp_path, deps, factory):
    clock = [NOW]
    app = create_app(
        data_dir=tmp_path / "data",
        deps_factory=lambda: deps,
        run_in_thread=False,
        source_factory=factory,
        now=lambda: clock[0],
    )
    client = TestClient(app)
    first = start(client)
    clock[0] = NOW + dt.timedelta(minutes=5)
    second = start(client)

    sessions = client.get("/api/directo").json()
    assert [s["session_id"] for s in sessions] == [second["session_id"], first["session_id"]]
    assert sessions[0]["estado"] == "terminado" and sessions[0]["afirmaciones"] == 2


def test_finished_sessions_survive_a_restart(tmp_path, deps, factory):
    data_dir = tmp_path / "data"
    app = create_app(
        data_dir=data_dir, deps_factory=lambda: deps, run_in_thread=False, source_factory=factory
    )
    session = start(TestClient(app))

    fresh = TestClient(create_app(data_dir=data_dir, deps_factory=lambda: deps))
    snapshot = fresh.get(f"/api/directo/{session['session_id']}").json()
    assert snapshot["estado"] == "terminado" and len(snapshot["informe"]["claims"]) == 2
    with fresh.stream("GET", f"/api/directo/{session['session_id']}/eventos") as response:
        body = "".join(response.iter_text())
    assert body.count("event: estado") == 1
    response = fresh.put(
        f"/api/directo/{session['session_id']}/oradores/speaker_1@0", json={"name": "Ana"}
    )
    assert response.json()["speaker_1@0"] == "Ana"


def test_an_interrupted_session_is_reported_as_an_error(tmp_path, deps):
    manager = LiveManager(tmp_path / "data", lambda: deps, run_in_thread=False)
    from pipeline.live.session import new_session

    state = new_session(URL, speech_date=DATE, block_seconds=60, started_at=NOW)
    manager.store.save(state)
    snapshot = manager.snapshot(state.config.session_id)
    assert snapshot["estado"] == "error" and "interrumpió" in snapshot["error"]


def test_stop_ends_a_running_session(tmp_path, deps, factory):
    app = create_app(data_dir=tmp_path / "data", deps_factory=lambda: deps, source_factory=factory)
    manager = app.state.live
    session_id = manager.start(LiveRequest(fuente=URL, fecha=DATE.isoformat()))
    manager.runners[session_id]._thread.join(timeout=10)
    assert manager.state(session_id).status == "terminado"
    response = TestClient(app).post(f"/api/directo/{session_id}/parar")
    assert response.status_code == 200
    assert manager.runners[session_id].source.stopped


def test_live_report_can_be_downloaded(client):
    session = start(client)
    response = client.get(f"/api/directo/{session['session_id']}/informe.html")
    assert response.status_code == 200
    assert "function renderReport" in response.text


def test_sse_format():
    event = LiveEvent(seq=3, at=NOW, type="evidencia", data={"id": "b000c001", "fuentes": 2})
    assert sse(event) == 'id: 3\nevent: evidencia\ndata: {"id": "b000c001", "fuentes": 2}\n\n'
