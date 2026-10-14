"""Sesión casi en vivo: estado acumulado, análisis de cada bloque y eventos para el panel.

Cada bloque de audio recorre los mismos pasos que un vídeo completo (transcripción, oradores,
afirmaciones, contexto, evidencia, veredictos y promesas) con las funciones del pipeline; el
estado de la sesión acumula los resultados y se guarda tras cada bloque, de modo que un panel
puede pintarlo en cualquier momento y una sesión interrumpida continúa donde quedó.
"""

import datetime as dt
import hashlib
import logging
import os
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from pipeline.claims import ClaimsError, detect_claims
from pipeline.context import resolve_context
from pipeline.costs import transcription_cost
from pipeline.evidence.base import collect_evidence
from pipeline.governments import Governments, default_governments
from pipeline.live.source import AudioBlock, BlockSource
from pipeline.models import (
    AudioInfo,
    ClaimDetection,
    ClaimEvidence,
    CostEntry,
    DetectedClaim,
    PromiseVerdict,
    Report,
    RunCost,
    Segment,
    SpeakerInfo,
    SpeakerMap,
    Transcript,
    Verdict,
)
from pipeline.promises import PromiseError, assess_promises
from pipeline.report import build_report, merge_claim, merge_promise
from pipeline.run import Deps, follow_up_search
from pipeline.speakers import identify_speakers, speaker_from_info
from pipeline.transcribe import TranscriptionConfigError, TranscriptionError
from pipeline.verdict import VerdictError, decide_verdicts, own_source_checker

logger = logging.getLogger(__name__)

SESSION_ID_LENGTH = 12
STATE_FILE = "state.json"
# Analyses that fail in a row (usage limit, network down…) before the session stops: going
# on would only spend searches on claims that never get a verdict.
MAX_CONSECUTIVE_FAILURES = 5
ANALYSIS_FAILED = "No se pudo completar el análisis de esta afirmación."
PROMISE_FAILED = "No se pudo completar el análisis de esta promesa."

SessionStatus = Literal["en_curso", "terminado", "parado", "error"]
BlockStatus = Literal["transcribiendo", "analizando", "hecho", "fallido"]
EventType = Literal[
    "bloque",
    "transcripcion",
    "oradores",
    "afirmaciones",
    "evidencia",
    "veredicto",
    "promesa",
    "aviso",
    "estado",
]
Emit = Callable[[EventType, dict[str, Any]], None]


class LiveError(RuntimeError):
    """The session cannot go on; the message is shown to the user."""


class BlockRecord(BaseModel):
    index: int
    start: float
    duration: float
    status: BlockStatus = "transcribiendo"
    error: str | None = None
    segments: int = 0
    claims: int = 0


class LiveEvent(BaseModel):
    seq: int
    at: dt.datetime
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)


class LiveConfig(BaseModel):
    session_id: str
    source: str  # stream URL or local file
    speech_date: dt.date
    block_seconds: int
    started_at: dt.datetime


class LiveState(BaseModel):
    """Everything the session has produced so far (state.json)."""

    config: LiveConfig
    status: SessionStatus = "en_curso"
    error: str | None = None
    blocks: list[BlockRecord] = Field(default_factory=list)
    # Cumulative transcript; times are seconds since the stream started and labels carry
    # the block ("speaker_0@3"): diarization labels do not persist across blocks.
    transcript: list[Segment] = Field(default_factory=list)
    speakers: SpeakerMap = Field(default_factory=SpeakerMap)
    # Who each label is, given by the user (editable while the session runs).
    speaker_info: dict[str, SpeakerInfo] = Field(default_factory=dict)
    detected: list[DetectedClaim] = Field(default_factory=list)
    verdicts: list[Verdict] = Field(default_factory=list)
    promises: list[PromiseVerdict] = Field(default_factory=list)
    cost: RunCost = Field(default_factory=RunCost)
    warnings: list[str] = Field(default_factory=list)

    def block(self, index: int) -> BlockRecord | None:
        return next((b for b in self.blocks if b.index == index), None)

    def decided_ids(self) -> set[str]:
        return {v.claim_id for v in self.verdicts} | {p.claim_id for p in self.promises}

    def pending(self) -> list[DetectedClaim]:
        """Detected claims whose verdict has not arrived yet."""
        decided = self.decided_ids()
        return [c for c in self.detected if c.id not in decided]

    def display_names(self) -> dict[str, str]:
        return {s.label: s.display() for s in self.speakers.speakers if s.name or s.party}

    def rename_speaker(self, label: str, info: SpeakerInfo, governments: Governments) -> None:
        """Set who a label is without calling the model; past and future claims use it."""
        self.speaker_info[label] = info
        speaker = speaker_from_info(label, info, governments)
        self.speakers.speakers = [
            *(s for s in self.speakers.speakers if s.label != label),
            speaker,
        ]

    def report(self, generated_at: dt.datetime) -> Report:
        """The claims decided so far, in the same shape as a finished job's report."""
        decided = self.decided_ids()
        return build_report(
            job_id=self.config.session_id,
            source=self.config.source,
            input_kind="video",
            speech_date=self.config.speech_date,
            speakers=self.display_names(),
            speaker_profiles=self.speakers.speakers,
            detection=ClaimDetection(claims=[c for c in self.detected if c.id in decided]),
            verdicts=self.verdicts,
            promises=self.promises,
            cost=self.cost,
            generated_at=generated_at,
            extra_warnings=self.warnings,
        )


def new_session(
    source: str,
    *,
    speech_date: dt.date,
    block_seconds: int,
    started_at: dt.datetime,
    speaker_info: dict[str, SpeakerInfo] | None = None,
) -> LiveState:
    digest = hashlib.sha256(f"{source}|{started_at.isoformat()}".encode()).hexdigest()
    config = LiveConfig(
        session_id=digest[:SESSION_ID_LENGTH],
        source=source,
        speech_date=speech_date,
        block_seconds=block_seconds,
        started_at=started_at,
    )
    return LiveState(config=config, speaker_info=speaker_info or {})


class LiveStore:
    """data/live/<session_id>/: state.json and the audio blocks."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def session_dir(self, session_id: str) -> Path:
        return self.root / session_id

    def blocks_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "blocks"

    def save(self, state: LiveState) -> Path:
        path = self.session_dir(state.config.session_id) / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(state.model_dump_json(indent=2).encode())
        os.replace(tmp, path)
        return path

    def load(self, session_id: str) -> LiveState | None:
        path = self.session_dir(session_id) / STATE_FILE
        if not path.exists():
            return None
        return LiveState.model_validate_json(path.read_bytes())

    def list_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / STATE_FILE).exists())


def _charge(state: LiveState, deps: Deps, step: str) -> None:
    state.cost.entries.extend([*deps.llm.take_costs(), *deps.take_search_cost(step)])


def transcribe_block(block: AudioBlock, deps: Deps) -> tuple[Transcript, CostEntry]:
    audio = AudioInfo(path=block.path, duration=block.duration, sample_rate=block.sample_rate)
    transcript = deps.transcriber.transcribe(audio)
    seconds = transcript.duration or block.duration
    return transcript, transcription_cost("transcript", transcript.provider, seconds)


def _absolute(block: AudioBlock, transcript: Transcript) -> Transcript:
    """The block's transcript with stream times and per-block speaker labels."""
    segments = [
        s.model_copy(
            update={
                "speaker": f"{s.speaker}@{block.index}",
                "start": (s.start or 0.0) + block.start,
                "end": (s.end or 0.0) + block.start,
                "words": [],
            }
        )
        for s in transcript.segments
    ]
    return Transcript(
        provider=transcript.provider,
        language=transcript.language,
        duration=transcript.duration,
        segments=segments,
    )


def _failed_verdict(claim: DetectedClaim, evidence: ClaimEvidence, error: str) -> Verdict:
    return Verdict(
        claim_id=claim.id,
        verdict="no_verificable",
        confidence=0.0,
        explanation=ANALYSIS_FAILED,
        analysis_error=error,
        evidence_errors=evidence.errors,
    )


def _failed_promise(claim: DetectedClaim, evidence: ClaimEvidence, error: str) -> PromiseVerdict:
    return PromiseVerdict(
        claim_id=claim.id,
        assessment="no_evaluable",
        confidence=0.0,
        explanation=PROMISE_FAILED,
        analysis_error=error,
        evidence_errors=evidence.errors,
    )


def analyze_block(
    block: AudioBlock,
    transcript: Transcript,
    state: LiveState,
    deps: Deps,
    *,
    governments: Governments,
    emit: Emit,
    failures: list[int] | None = None,
) -> None:
    """Run every step after transcription on one block and fold the results into `state`.

    `failures` (one-element list) counts analyses that failed in a row across blocks;
    after MAX_CONSECUTIVE_FAILURES the session stops with LiveError. Configuration errors
    propagate; a step that fails for the whole block marks it `fallido` and the session
    goes on with the next one.
    """
    date = state.config.speech_date
    record = state.block(block.index)
    if record is None:
        record = BlockRecord(index=block.index, start=block.start, duration=block.duration)
        state.blocks.append(record)
    record.status = "analizando"
    emit("bloque", record.model_dump())

    local = _absolute(block, transcript)
    offset = len(state.transcript)
    state.transcript.extend(local.segments)
    record.segments = len(local.segments)
    emit(
        "transcripcion",
        {"bloque": block.index, "segmentos": [s.model_dump() for s in local.segments]},
    )

    found = identify_speakers(
        local,
        speech_date=date,
        llm=deps.llm,
        model=deps.model_for("detection"),
        governments=governments,
        info=state.speaker_info,
    )
    _charge(state, deps, "speakers")
    known = {s.label for s in state.speakers.speakers}
    state.speakers.speakers.extend(s for s in found.speakers if s.label not in known)
    state.speakers.errors.extend(found.errors)
    emit("oradores", {"oradores": state.display_names()})

    try:
        detection = detect_claims(
            local,
            speech_date=date,
            input_kind="video",
            llm=deps.llm,
            model=deps.model_for("detection"),
        )
    except ClaimsError as exc:
        _charge(state, deps, "claims")
        record.status, record.error = "fallido", str(exc)
        state.warnings.append(f"Bloque {block.index}: {exc}")
        emit("bloque", record.model_dump())
        return
    _charge(state, deps, "claims")
    for failure in detection.failed_blocks:
        state.warnings.append(f"Bloque {block.index}: un tramo sin analizar ({failure.error})")
    detection.claims = [
        c.model_copy(update={"id": f"b{block.index:03d}{c.id}"}) for c in detection.claims
    ]
    emit(
        "afirmaciones",
        {
            "bloque": block.index,
            "afirmaciones": [
                c.model_copy(update={"segment": _shift(c.segment, offset)}).model_dump()
                for c in detection.claims
            ],
        },
    )

    resolved = resolve_context(
        detection,
        local,
        state.speakers,
        speech_date=date,
        llm=deps.llm,
        model=deps.model_for("verdict"),
        governments=governments,
    )
    _charge(state, deps, "context")
    claims = [c.model_copy(update={"segment": _shift(c.segment, offset)}) for c in resolved.claims]
    state.detected.extend(claims)
    record.claims = len(claims)

    failures = failures if failures is not None else [0]
    for claim in claims:
        evidence = ClaimEvidence(claim_id=claim.id)
        # Unresolved claims are no_verificable anyway: don't spend searches on them.
        if claim.kind == "promesa" or claim.context_status != "no_resuelto":
            [evidence] = collect_evidence([claim], deps.evidence_sources, date)
            _charge(state, deps, "evidence")
            emit(
                "evidencia",
                {"id": claim.id, "fuentes": len(evidence.items), "errores": evidence.errors},
            )
        if claim.kind == "promesa":
            try:
                [promise] = assess_promises(
                    [claim],
                    [evidence],
                    speech_date=date,
                    llm=deps.llm,
                    model=deps.model_for("verdict"),
                )
            except PromiseError as exc:
                promise = _failed_promise(claim, evidence, str(exc))
            _charge(state, deps, "promises")
            state.promises.append(promise)
            emit("promesa", {"promesa": merge_promise(claim, promise).model_dump(mode="json")})
            error = promise.analysis_error
        else:
            try:
                [verdict] = decide_verdicts(
                    [claim],
                    [evidence],
                    speech_date=date,
                    llm=deps.llm,
                    model=deps.model_for("verdict"),
                    follow_up=follow_up_search(deps, date),
                    own_source=own_source_checker(state.speakers, governments, date),
                    thinking=deps.verdict_thinking,
                )
            except VerdictError as exc:
                verdict = _failed_verdict(claim, evidence, str(exc))
            _charge(state, deps, "verdicts")
            state.verdicts.append(verdict)
            emit("veredicto", {"afirmacion": merge_claim(claim, verdict).model_dump(mode="json")})
            error = verdict.analysis_error
        failures[0] = failures[0] + 1 if error else 0
        if failures[0] >= MAX_CONSECUTIVE_FAILURES:
            raise LiveError(f"{failures[0]} análisis seguidos han fallado; último error: {error}")

    record.status = "hecho"
    emit("bloque", record.model_dump())


def _shift(segment: int | None, offset: int) -> int | None:
    return None if segment is None else segment + offset


class LiveRunner:
    """Drives one session: transcribes blocks in a thread while this one analyses them.

    Events are kept in memory for the panel (`wait_for`); the state is saved after each
    block. `lock` guards the state for readers in other threads (the web app)."""

    def __init__(
        self,
        state: LiveState,
        deps: Deps,
        source: BlockSource,
        *,
        store: LiveStore,
        governments: Governments | None = None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.state = state
        self.deps = deps
        self.source = source
        self.store = store
        self.governments = governments or default_governments()
        self.now = now
        self.events: list[LiveEvent] = []
        self.lock = threading.RLock()
        self._new_event = threading.Condition(self.lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- events -------------------------------------------------------------

    def emit(self, type_: EventType, data: dict[str, Any]) -> None:
        with self._new_event:
            self.events.append(
                LiveEvent(seq=len(self.events) + 1, at=self.now(), type=type_, data=data)
            )
            self._new_event.notify_all()

    def events_since(self, seq: int) -> list[LiveEvent]:
        with self.lock:
            return self.events[seq:]

    def wait_for(self, seq: int, timeout: float) -> list[LiveEvent]:
        """Events after `seq`, waiting up to `timeout` seconds for new ones."""
        with self._new_event:
            if len(self.events) <= seq:
                self._new_event.wait(timeout)
            return self.events[seq:]

    @property
    def finished(self) -> bool:
        return self.state.status != "en_curso"

    # --- running ------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.run, daemon=True, name=f"directo-{self.state.config.session_id}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.source.stop()

    def run(self) -> None:
        state = self.state
        # A resumed session numbers new blocks after the ones it already has.
        base_index = len(state.blocks)
        base_start = sum(b.duration for b in state.blocks)
        pending: queue.Queue[Any] = queue.Queue(maxsize=4)

        def produce() -> None:
            try:
                for raw in self.source.blocks():
                    if self._stop.is_set():
                        break
                    block = raw.model_copy(
                        update={"index": raw.index + base_index, "start": raw.start + base_start}
                    )
                    record = BlockRecord(
                        index=block.index, start=block.start, duration=block.duration
                    )
                    with self.lock:
                        state.blocks.append(record)
                    self.emit("bloque", record.model_dump())
                    try:
                        transcript, cost = transcribe_block(block, self.deps)
                    except TranscriptionConfigError:
                        raise
                    except TranscriptionError as exc:
                        logger.warning("Bloque %d sin transcribir: %s", block.index, exc)
                        with self.lock:
                            record.status, record.error = "fallido", str(exc)
                            state.warnings.append(f"Bloque {block.index}: {exc}")
                        self.emit("bloque", record.model_dump())
                        continue
                    pending.put((block, transcript, cost))
            except BaseException as exc:  # handed to the analysing thread, which reports it
                pending.put(exc)
            finally:
                pending.put(None)

        producer = threading.Thread(target=produce, daemon=True, name="directo-transcripcion")
        producer.start()
        failures = [0]
        try:
            while True:
                item = pending.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                block, transcript, cost = item
                state.cost.entries.append(cost)
                analyze_block(
                    block,
                    transcript,
                    state,
                    self.deps,
                    governments=self.governments,
                    emit=self.emit,
                    failures=failures,
                )
                with self.lock:
                    self.store.save(state)
            state.status = "parado" if self._stop.is_set() else "terminado"
        except Exception as exc:
            logger.exception("La sesión en directo %s falló", state.config.session_id)
            self.source.stop()
            state.status, state.error = "error", str(exc) or type(exc).__name__
        finally:
            with self.lock:
                self.store.save(state)
            self.emit("estado", {"estado": state.status, "error": state.error})
