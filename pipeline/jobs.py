"""Carpeta de trabajo data/<job_id>/, manifest y ejecución reanudable de pasos."""

import datetime as dt
import hashlib
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from pipeline.models import STEPS, CostEntry, InputKind, JobManifest, StepName, StepRecord

logger = logging.getLogger(__name__)

JOB_ID_LENGTH = 12
MANIFEST_FILE = "manifest.json"


class JobError(RuntimeError):
    """Problem with a job's folder or step plan; the message is shown to the user."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prompt_hash(prompts_dir: Path, name: str) -> str:
    path = prompts_dir / name
    if not path.is_file():
        raise JobError(f"No existe el prompt {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def earliest_step(*steps: StepName | None) -> StepName | None:
    present = [s for s in steps if s is not None]
    return min(present, key=STEPS.index) if present else None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class JobStore:
    """Reads and writes one job's intermediate files and its manifest."""

    def __init__(self, root: Path, manifest: JobManifest) -> None:
        self.root = root
        self.manifest = manifest

    @classmethod
    def open_or_create(
        cls,
        data_dir: Path,
        input_path: Path,
        input_kind: InputKind,
        speech_date: dt.date,
        speakers: dict[str, str] | None = None,
    ) -> tuple["JobStore", StepName | None]:
        """Open the job for this input file (video or text), creating it if needed.

        Also returns the earliest step invalidated by changed parameters: a new
        speech date affects claim detection onwards, new speaker names only the report.
        `speakers=None` keeps the names already stored.
        """
        sha = file_sha256(input_path)
        job_id = sha[:JOB_ID_LENGTH]
        root = data_dir / job_id
        manifest_path = root / MANIFEST_FILE

        if not manifest_path.exists():
            root.mkdir(parents=True, exist_ok=True)
            manifest = JobManifest(
                job_id=job_id,
                input_path=str(input_path),
                input_sha256=sha,
                input_kind=input_kind,
                speech_date=speech_date,
                speakers=speakers or {},
                created_at=_now(),
            )
            store = cls(root, manifest)
            store.save_manifest()
            return store, None

        manifest = JobManifest.model_validate_json(manifest_path.read_text())
        invalidated: StepName | None = None
        if manifest.speech_date != speech_date:
            invalidated = "claims"
        elif speakers is not None and speakers != manifest.speakers:
            invalidated = "report"

        manifest.input_path = str(input_path)
        manifest.speech_date = speech_date
        if speakers is not None:
            manifest.speakers = speakers
        store = cls(root, manifest)
        store.save_manifest()
        return store, invalidated

    @property
    def job_id(self) -> str:
        return self.manifest.job_id

    def path(self, name: str) -> Path:
        return self.root / name

    def write(self, name: str, value: Any, type_: Any) -> Path:
        path = self.path(name)
        _atomic_write(path, TypeAdapter(type_).dump_json(value, indent=2))
        return path

    def read(self, name: str, type_: Any) -> Any:
        path = self.path(name)
        if not path.exists():
            raise JobError(f"Falta {path}; vuelve a ejecutar los pasos anteriores")
        return TypeAdapter(type_).validate_json(path.read_bytes())

    def save_manifest(self) -> None:
        _atomic_write(self.path(MANIFEST_FILE), self.manifest.model_dump_json(indent=2).encode())


@dataclass(frozen=True)
class StepOutcome:
    cost: list[CostEntry] = field(default_factory=list)
    # False when some items failed transiently: the output is kept (and reported) but the
    # step is run again next time instead of being reused.
    complete: bool = True


@dataclass(frozen=True)
class Step:
    """A pipeline step: reads its inputs from the store and writes its outputs there."""

    name: StepName
    run: Callable[[JobStore], StepOutcome]
    prompts: tuple[str, ...] = ()
    # Bump when the step's code changes its output (e.g. how text is split into turns), so
    # cached results are recomputed like after a prompt change.
    version: str = ""


def _current_hashes(step: Step, prompts_dir: Path) -> dict[str, str]:
    hashes = {name: prompt_hash(prompts_dir, name) for name in step.prompts}
    if step.version:
        hashes["version"] = step.version
    return hashes


def _ordered(steps: Sequence[Step]) -> list[Step]:
    return sorted(steps, key=lambda s: STEPS.index(s.name))


def plan_steps(
    manifest: JobManifest,
    steps: Sequence[Step],
    prompts_dir: Path,
    from_step: StepName | None = None,
) -> list[StepName]:
    """Names of the steps to run, in order.

    A step runs if it was never completed, if it is `from_step` or later, or if one
    of its prompts changed; once a step runs, every later step runs too.
    """
    names = [s.name for s in steps]
    if from_step is not None and from_step not in names:
        raise JobError(f"Paso desconocido: {from_step}")

    # Hash every prompt up front so a missing one fails before any paid step runs.
    hashes = {s.name: _current_hashes(s, prompts_dir) for s in steps}

    to_run: list[StepName] = []
    must_run = False
    for step in _ordered(steps):
        if step.name == from_step:
            must_run = True
        record = manifest.steps.get(step.name)
        prompt_changed = (
            record is not None
            and record.status == "done"
            and record.prompt_hashes != hashes[step.name]
        )
        if prompt_changed:
            logger.info("El prompt o el código del paso %s ha cambiado; se recalcula", step.name)
        stale = record is None or record.status != "done" or prompt_changed
        if must_run or stale:
            to_run.append(step.name)
            must_run = True
    return to_run


def run_steps(
    store: JobStore,
    steps: Sequence[Step],
    prompts_dir: Path,
    from_step: StepName | None = None,
    on_step: Callable[[StepName], None] | None = None,
) -> list[StepName]:
    """Run the planned steps, recording status, prompt hashes and cost in the manifest.

    A failing step is recorded as failed and the exception is re-raised; failures of
    individual claims must be handled inside the step. `on_step` is told each step that is
    about to run (progress shown in the panel).
    """
    to_run = plan_steps(store.manifest, steps, prompts_dir, from_step)
    for step in _ordered(steps):
        if step.name not in to_run:
            logger.info("Paso %s: reutilizado de %s", step.name, store.root)
            continue

        # Drop this and later records first so a crash never leaves stale "done" entries.
        for later in STEPS[STEPS.index(step.name) :]:
            store.manifest.steps.pop(later, None)
        store.save_manifest()

        hashes = _current_hashes(step, prompts_dir)
        logger.info("Paso %s: ejecutando", step.name)
        if on_step is not None:
            on_step(step.name)
        started = time.monotonic()
        try:
            outcome = step.run(store)
        except Exception as exc:
            store.manifest.steps[step.name] = StepRecord(
                status="failed",
                finished_at=_now(),
                prompt_hashes=hashes,
                # Steps attach what they already spent before failing.
                cost=list(getattr(exc, "step_costs", [])),
                error=f"{type(exc).__name__}: {exc}",
                seconds=round(time.monotonic() - started, 1),
            )
            store.save_manifest()
            raise
        if not outcome.complete:
            logger.warning("Paso %s incompleto: se repetirá en la próxima ejecución", step.name)
        store.manifest.steps[step.name] = StepRecord(
            status="done" if outcome.complete else "partial",
            finished_at=_now(),
            prompt_hashes=hashes,
            cost=outcome.cost,
            seconds=round(time.monotonic() - started, 1),
        )
        store.save_manifest()
    return to_run
