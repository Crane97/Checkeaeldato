import datetime as dt
from pathlib import Path

import pytest

from pipeline.jobs import (
    JOB_ID_LENGTH,
    JobError,
    JobStore,
    Step,
    StepOutcome,
    earliest_step,
    plan_steps,
    run_steps,
)
from pipeline.models import STEPS, CostEntry, Segment

DATE = dt.date(2026, 11, 20)


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "debate.mp4"
    path.write_bytes(b"fake video bytes")
    return path


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    path = tmp_path / "prompts"
    path.mkdir()
    (path / "deteccion.md").write_text("v1")
    return path


@pytest.fixture
def store(tmp_path: Path, video: Path) -> JobStore:
    store, _ = JobStore.open_or_create(tmp_path / "data", video, "video", DATE)
    return store


class Recorder:
    """Builds fake steps that log their calls and can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_on: str | None = None

    def steps(self) -> list[Step]:
        return [
            Step(name, self._runner(name), ("deteccion.md",) if name == "claims" else ())
            for name in STEPS
        ]

    def _runner(self, name: str):
        def run(store: JobStore) -> StepOutcome:
            self.calls.append(name)
            if name == self.fail_on:
                raise RuntimeError("boom")
            return StepOutcome(cost=[CostEntry(step=name, provider="fake", usd=0.01)])

        return run


def test_job_id_depends_only_on_video_content(tmp_path: Path, video: Path):
    store1, _ = JobStore.open_or_create(tmp_path / "data", video, "video", DATE)
    copy = tmp_path / "copia.mp4"
    copy.write_bytes(video.read_bytes())
    store2, _ = JobStore.open_or_create(tmp_path / "data", copy, "video", DATE)
    assert store1.job_id == store2.job_id
    assert len(store1.job_id) == JOB_ID_LENGTH
    assert store2.manifest.input_path == str(copy)

    other = tmp_path / "otro.mp4"
    other.write_bytes(b"different")
    store3, _ = JobStore.open_or_create(tmp_path / "data", other, "video", DATE)
    assert store3.job_id != store1.job_id


def test_reopening_detects_changed_parameters(tmp_path: Path, video: Path):
    data = tmp_path / "data"
    JobStore.open_or_create(data, video, "video", DATE, {"speaker_0": "A"})

    _, invalidated = JobStore.open_or_create(data, video, "video", DATE)
    assert invalidated is None

    store, invalidated = JobStore.open_or_create(data, video, "video", DATE, {"speaker_0": "B"})
    assert invalidated == "report"
    assert store.manifest.speakers == {"speaker_0": "B"}

    store, invalidated = JobStore.open_or_create(data, video, "video", dt.date(2026, 11, 21))
    assert invalidated == "claims"
    # speakers=None keeps the stored names
    assert store.manifest.speakers == {"speaker_0": "B"}


def test_write_and_read_roundtrip(store: JobStore):
    segments = [Segment(speaker="speaker_0", start=0, end=1, text="hola")]
    store.write("transcript.json", segments, list[Segment])
    assert store.read("transcript.json", list[Segment]) == segments
    assert not list(store.root.glob("*.tmp"))


def test_reading_missing_file_raises_job_error(store: JobStore):
    with pytest.raises(JobError, match="claims.json"):
        store.read("claims.json", list[Segment])


def test_first_run_executes_everything_and_second_run_nothing(store, prompts_dir):
    rec = Recorder()
    assert run_steps(store, rec.steps(), prompts_dir) == list(STEPS)
    assert rec.calls == list(STEPS)

    rec.calls.clear()
    assert run_steps(store, rec.steps(), prompts_dir) == []
    assert rec.calls == []


def test_from_step_reruns_that_step_and_later(store, prompts_dir):
    rec = Recorder()
    run_steps(store, rec.steps(), prompts_dir)
    rec.calls.clear()

    run_steps(store, rec.steps(), prompts_dir, from_step="evidence")
    assert rec.calls == ["evidence", "verdicts", "report"]


def test_changed_prompt_reruns_from_its_step(store, prompts_dir):
    rec = Recorder()
    run_steps(store, rec.steps(), prompts_dir)
    (prompts_dir / "deteccion.md").write_text("v2")

    assert plan_steps(store.manifest, rec.steps(), prompts_dir) == [
        "claims",
        "evidence",
        "verdicts",
        "report",
    ]


def test_failure_is_recorded_and_resume_starts_at_failed_step(store, prompts_dir):
    rec = Recorder()
    run_steps(store, rec.steps(), prompts_dir)

    rec.fail_on = "evidence"
    with pytest.raises(RuntimeError, match="boom"):
        run_steps(store, rec.steps(), prompts_dir, from_step="claims")

    reloaded, _ = JobStore.open_or_create(
        store.root.parent, Path(store.manifest.input_path), "video", DATE
    )
    steps = reloaded.manifest.steps
    assert steps["evidence"].status == "failed"
    assert "boom" in (steps["evidence"].error or "")
    # Later steps from the previous run are no longer marked as done.
    assert "verdicts" not in steps and "report" not in steps

    rec.fail_on = None
    rec.calls.clear()
    run_steps(reloaded, rec.steps(), prompts_dir)
    assert rec.calls == ["evidence", "verdicts", "report"]


def test_manifest_cost_covers_current_outputs(store, prompts_dir):
    rec = Recorder()
    run_steps(store, rec.steps(), prompts_dir)
    run_steps(store, rec.steps(), prompts_dir, from_step="report")
    # One entry per step, not duplicated by the rerun.
    assert store.manifest.run_cost().total_usd == pytest.approx(0.06)


def test_unknown_from_step_and_missing_prompt_raise(store, prompts_dir):
    rec = Recorder()
    with pytest.raises(JobError, match="Paso desconocido"):
        plan_steps(store.manifest, rec.steps()[:2], prompts_dir, from_step="report")

    (prompts_dir / "deteccion.md").unlink()
    with pytest.raises(JobError, match="deteccion.md"):
        run_steps(store, rec.steps(), prompts_dir)
    # Nothing ran: a missing prompt must fail before paid steps like transcription.
    assert rec.calls == []


def test_earliest_step():
    assert earliest_step(None, "report", "claims") == "claims"
    assert earliest_step(None, None) is None


def test_failed_step_keeps_the_cost_it_already_spent(store, prompts_dir):
    def failing(store: JobStore) -> StepOutcome:
        exc = RuntimeError("todos los bloques fallaron")
        exc.step_costs = [CostEntry(step="claims", provider="anthropic", usd=0.05)]
        raise exc

    steps = [Step("claims", failing, ("deteccion.md",))]
    with pytest.raises(RuntimeError):
        run_steps(store, steps, prompts_dir)
    record = store.manifest.steps["claims"]
    assert record.status == "failed"
    assert [c.usd for c in record.cost] == [0.05]
