import datetime as dt
from pathlib import Path

import pytest

from pipeline.live.session import (
    MAX_CONSECUTIVE_FAILURES,
    LiveRunner,
    LiveState,
    LiveStore,
    new_session,
)
from pipeline.live.source import AudioBlock
from pipeline.llm import LLMConfigError
from pipeline.models import SpeakerInfo
from pipeline.transcribe import TranscriptionConfigError, TranscriptionError
from tests.test_run import DATE, FakeDeps

STARTED = dt.datetime(2026, 11, 20, 21, 0, tzinfo=dt.UTC)
URL = "https://emisora.example/directo.m3u8"


def make_blocks(tmp_path: Path, n: int = 2, seconds: float = 60.0) -> list[AudioBlock]:
    blocks = []
    for i in range(n):
        path = tmp_path / f"block_{i:05d}.flac"
        path.write_bytes(b"fLaC")
        blocks.append(AudioBlock(index=i, path=str(path), start=i * seconds, duration=seconds))
    return blocks


class FakeBlockSource:
    def __init__(self, blocks: list[AudioBlock]) -> None:
        self.items = blocks
        self.stopped = False

    def blocks(self):
        for block in self.items:
            if self.stopped:
                return
            yield block

    def stop(self) -> None:
        self.stopped = True


class FailingTranscriber:
    name = "fake"

    def __init__(self, error: Exception, fail_index: int = 0) -> None:
        self.error, self.fail_index, self.calls = error, fail_index, 0

    def transcribe(self, audio):
        from tests.test_run import FakeTranscriber

        self.calls += 1
        if self.calls - 1 == self.fail_index:
            raise self.error
        return FakeTranscriber().transcribe(audio)


@pytest.fixture
def state() -> LiveState:
    return new_session(URL, speech_date=DATE, block_seconds=60, started_at=STARTED)


def run(tmp_path: Path, state: LiveState, deps: FakeDeps, blocks: list[AudioBlock]) -> LiveRunner:
    runner = LiveRunner(state, deps, FakeBlockSource(blocks), store=LiveStore(tmp_path / "live"))
    runner.run()
    return runner


def test_session_id_depends_on_source_and_start(state):
    other = new_session(URL, speech_date=DATE, block_seconds=60, started_at=STARTED)
    later = new_session(
        URL, speech_date=DATE, block_seconds=60, started_at=STARTED + dt.timedelta(seconds=1)
    )
    assert len(state.config.session_id) == 12
    assert state.config.session_id == other.config.session_id
    assert state.config.session_id != later.config.session_id


def test_claims_get_unique_ids_and_stream_times(tmp_path, state):
    deps = FakeDeps()
    run(tmp_path, state, deps, make_blocks(tmp_path, 2))

    assert state.status == "terminado"
    assert [c.id for c in state.detected] == ["b000c001", "b001c001"]
    assert [c.start for c in state.detected] == [3.0, 63.0]
    # Segment indices point into the cumulative transcript; labels carry the block.
    assert [c.segment for c in state.detected] == [1, 3]
    assert [s.speaker for s in state.transcript] == [
        "speaker_0@0",
        "speaker_1@0",
        "speaker_0@1",
        "speaker_1@1",
    ]
    assert state.transcript[3].start == 63.0 and state.transcript[3].end == 66.0
    assert [(b.index, b.status, b.claims) for b in state.blocks] == [
        (0, "hecho", 1),
        (1, "hecho", 1),
    ]
    assert [v.verdict for v in state.verdicts] == ["falso", "falso"]


def test_events_follow_the_life_of_a_claim(tmp_path, state):
    runner = run(tmp_path, state, FakeDeps(), make_blocks(tmp_path, 1))

    types = [e.type for e in runner.events]
    assert types == [
        "bloque",  # transcribiendo
        "bloque",  # analizando
        "transcripcion",
        "oradores",
        "afirmaciones",
        "evidencia",
        "veredicto",
        "bloque",  # hecho
        "estado",
    ]
    assert [e.seq for e in runner.events] == list(range(1, 10))
    verdict = next(e for e in runner.events if e.type == "veredicto").data["afirmacion"]
    assert verdict["id"] == "b000c001" and verdict["category"] == "falso_con_fuente_oficial"
    assert runner.events[-1].data == {"estado": "terminado", "error": None}
    assert runner.events_since(7) == runner.events[7:]
    assert runner.wait_for(9, timeout=0.01) == []


def test_report_has_only_decided_claims_and_running_totals(tmp_path, state):
    run(tmp_path, state, FakeDeps(), make_blocks(tmp_path, 2))

    report = state.report(STARTED)
    assert report.job_id == state.config.session_id and report.input_kind == "video"
    assert report.by_category["falso_con_fuente_oficial"] == 2
    assert state.pending() == []
    assert report.cost.total_usd > 0
    # Transcription of each block is charged, even though "fake" has no price.
    assert [e.step for e in report.cost.entries if e.provider == "fake"] == ["transcript"] * 2


def test_a_block_that_cannot_be_transcribed_is_skipped(tmp_path, state):
    deps = FakeDeps()
    deps.transcriber = FailingTranscriber(TranscriptionError("audio vacío"), fail_index=0)
    run(tmp_path, state, deps, make_blocks(tmp_path, 2))

    assert state.status == "terminado"
    assert [(b.index, b.status) for b in state.blocks] == [(0, "fallido"), (1, "hecho")]
    assert state.blocks[0].error == "audio vacío"
    assert state.warnings == ["Bloque 0: audio vacío"]
    assert [c.id for c in state.detected] == ["b001c001"]


def test_configuration_errors_stop_the_session(tmp_path, state):
    deps = FakeDeps()
    deps.transcriber = FailingTranscriber(TranscriptionConfigError("clave inválida"))
    runner = run(tmp_path, state, deps, make_blocks(tmp_path, 2))

    assert state.status == "error" and "clave inválida" in state.error
    assert runner.events[-1].type == "estado"
    assert runner.source.stopped


def test_llm_configuration_errors_stop_the_session(tmp_path, state):
    deps = FakeDeps()

    def broken(**kwargs):
        raise LLMConfigError("No hay credenciales de Anthropic")

    deps.llm.structured = broken
    run(tmp_path, state, deps, make_blocks(tmp_path, 1))
    assert state.status == "error" and "credenciales" in state.error


def test_too_many_failed_analyses_stop_the_session(tmp_path, state):
    from pipeline.llm import LLMError
    from tests.test_run import FakeLLM

    class FlakyVerdicts(FakeLLM):
        def structured(self, *, step, output_type, **kwargs):
            if step == "verdicts":
                raise LLMError("límite de uso")
            return super().structured(step=step, output_type=output_type, **kwargs)

    deps = FakeDeps()
    deps.llm = FlakyVerdicts()
    run(tmp_path, state, deps, make_blocks(tmp_path, MAX_CONSECUTIVE_FAILURES + 2))

    assert state.status == "error" and "seguidos" in state.error
    assert len(state.verdicts) == MAX_CONSECUTIVE_FAILURES
    assert all(v.analysis_error for v in state.verdicts)


def test_renaming_a_speaker_needs_no_model_call(tmp_path, state):
    from pipeline.governments import default_governments

    deps = FakeDeps()
    run(tmp_path, state, deps, make_blocks(tmp_path, 1))
    calls = list(deps.llm.calls)

    state.rename_speaker(
        "speaker_1@0", SpeakerInfo(name="Ana Ruiz", party="PSOE"), default_governments()
    )

    assert deps.llm.calls == calls
    assert state.display_names()["speaker_1@0"] == "Ana Ruiz (PSOE)"
    report = state.report(STARTED)
    assert report.speakers["speaker_1@0"] == "Ana Ruiz (PSOE)"
    assert report.by_speaker == {"Ana Ruiz (PSOE)": report.by_speaker["Ana Ruiz (PSOE)"]}


def test_speaker_info_given_up_front_is_applied_to_new_blocks(tmp_path):
    state = new_session(
        URL,
        speech_date=DATE,
        block_seconds=60,
        started_at=STARTED,
        speaker_info={"speaker_1@0": SpeakerInfo(name="Luis Gómez", party="PP")},
    )
    run(tmp_path, state, FakeDeps(), make_blocks(tmp_path, 1))
    assert state.display_names()["speaker_1@0"] == "Luis Gómez (PP)"


def test_state_is_saved_after_each_block_and_resumes_with_new_numbering(tmp_path, state):
    store = LiveStore(tmp_path / "live")
    run(tmp_path, state, FakeDeps(), make_blocks(tmp_path, 1))
    assert store.list_ids() == [state.config.session_id]

    loaded = store.load(state.config.session_id)
    assert loaded == state
    loaded.status = "en_curso"
    runner = LiveRunner(loaded, FakeDeps(), FakeBlockSource(make_blocks(tmp_path, 1)), store=store)
    runner.run()

    assert [b.index for b in loaded.blocks] == [0, 1]
    assert [c.id for c in loaded.detected] == ["b000c001", "b001c001"]
    assert loaded.detected[1].start == 63.0
    assert store.load(state.config.session_id) == loaded


def test_stop_ends_the_session_after_the_current_block(tmp_path, state):
    blocks = make_blocks(tmp_path, 3)
    source = FakeBlockSource(blocks)
    runner = LiveRunner(state, FakeDeps(), source, store=LiveStore(tmp_path / "live"))
    runner.stop()
    runner.run()
    assert state.status == "parado" and state.blocks == []
    assert runner.finished


def test_missing_state_loads_as_none(tmp_path):
    store = LiveStore(tmp_path / "live")
    assert store.load("000000000000") is None and store.list_ids() == []
