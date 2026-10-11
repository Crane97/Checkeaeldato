"""Montaje y ejecución del pipeline completo para una entrada (vídeo o texto)."""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Protocol

from pipeline.audio import extract_audio, purge_audio
from pipeline.claims import PROMPT as CLAIMS_PROMPT
from pipeline.claims import detect_claims
from pipeline.config import ConfigError, LlmStep, Settings
from pipeline.context import PROMPT as CONTEXT_PROMPT
from pipeline.context import resolve_context
from pipeline.costs import transcription_cost
from pipeline.evidence.base import EvidenceSource, collect_evidence
from pipeline.governments import default_governments
from pipeline.jobs import JobStore, Step, StepOutcome, earliest_step, run_steps
from pipeline.models import (
    AudioInfo,
    ClaimDetection,
    ClaimEvidence,
    DetectedClaim,
    EvidenceItem,
    EvidenceRun,
    InputKind,
    Report,
    SpeakerInfo,
    SpeakerMap,
    StepName,
    Transcript,
    Verdict,
)
from pipeline.report import build_report, format_summary
from pipeline.speakers import PROMPT as SPEAKERS_PROMPT
from pipeline.speakers import identify_speakers
from pipeline.text_input import SUPPORTED_SUFFIXES, TEXT_SPEAKER, load_text
from pipeline.transcribe import Transcriber
from pipeline.verdict import PROMPT as VERDICT_PROMPT
from pipeline.verdict import FollowUpSearch, decide_verdicts

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SPEAKER_INFO_FILE = "speakers_input.json"


class Deps(Protocol):
    """External services used by the steps; created lazily so resuming is free."""

    llm: Any
    transcriber: Transcriber
    evidence_sources: list[EvidenceSource]
    disabled_sources: list[str]

    def model_for(self, step: LlmStep) -> str: ...


class LiveDeps:
    """Real clients built from settings, only when a step needs them."""

    def __init__(self, settings: Settings, prompts_dir: Path = PROMPTS_DIR) -> None:
        self.settings = settings
        self.prompts_dir = prompts_dir

    def model_for(self, step: LlmStep) -> str:
        return self.settings.model_for(step)

    @cached_property
    def llm(self) -> Any:
        if self.settings.llm_backend == "claude-code":
            from pipeline.llm_claude_code import ClaudeCodeLLM

            return ClaudeCodeLLM(self.prompts_dir)
        from pipeline.llm import LLMClient, make_anthropic_client

        return LLMClient(make_anthropic_client(self.settings), self.prompts_dir)

    @cached_property
    def transcriber(self) -> Transcriber:
        from pipeline.transcribe import make_transcriber

        return make_transcriber(self.settings)

    @cached_property
    def _sources(self) -> tuple[list[EvidenceSource], list[str]]:
        from pipeline.evidence.factcheck_api import FactCheckSource
        from pipeline.evidence.oficiales import OfficialSearchSource
        from pipeline.evidence.tavily import TavilyClient
        from pipeline.evidence.web import WebSearchSource

        sources: list[EvidenceSource] = []
        disabled: list[str] = []
        if self.settings.google_factcheck_api_key:
            sources.append(FactCheckSource(self.settings.secret("google_factcheck_api_key")))
        else:
            disabled.append("factcheck")
        if self.settings.tavily_api_key:
            tavily = TavilyClient(self.settings.secret("tavily_api_key"))
            sources += [OfficialSearchSource(tavily), WebSearchSource(tavily)]
        else:
            disabled += ["oficial", "web"]
        if not sources:
            raise ConfigError(
                "No hay ninguna fuente de evidencia configurada: define "
                "GOOGLE_FACTCHECK_API_KEY y/o TAVILY_API_KEY en .env"
            )
        return sources, disabled

    @property
    def evidence_sources(self) -> list[EvidenceSource]:
        return self._sources[0]

    @property
    def disabled_sources(self) -> list[str]:
        return self._sources[1]


@dataclass(frozen=True)
class JobResult:
    report: Report
    report_path: Path
    summary: str
    steps_run: list[StepName]


def input_kind_for(path: Path) -> InputKind:
    return "texto" if path.suffix.lower() in SUPPORTED_SUFFIXES else "video"


def _with_llm_costs(deps: Deps, work: Callable[[], None]) -> StepOutcome:
    """Run an LLM step and return its cost, attaching it to the error if it fails."""
    try:
        work()
    except Exception as exc:
        exc.step_costs = deps.llm.take_costs()  # type: ignore[attr-defined]
        raise
    return StepOutcome(cost=deps.llm.take_costs())


def follow_up_search(deps: Deps, speech_date: dt.date) -> FollowUpSearch:
    """Targeted second search with the queries Claude asks for when it cannot decide.
    Fact-checks are left out: they match whole claims, not the missing data."""

    def search(
        claim: DetectedClaim, queries: list[str], first_id: int
    ) -> tuple[list[EvidenceItem], list[str]]:
        sources = [s for s in deps.evidence_sources if s.name != "factcheck"]
        if not sources:
            return [], []
        probe = claim.model_copy(update={"search_queries": queries})
        [found] = collect_evidence([probe], sources, speech_date)
        items = [
            item.model_copy(update={"id": f"E{first_id + n}"}) for n, item in enumerate(found.items)
        ]
        return items, [f"búsqueda adicional: {e}" for e in found.errors]

    return search


def build_steps(
    store: JobStore, input_path: Path, deps: Deps, *, purge_audio_after: bool = False
) -> list[Step]:
    manifest = store.manifest
    kind = manifest.input_kind

    def audio_step(store: JobStore) -> StepOutcome:
        store.write("audio.json", extract_audio(input_path, store.root), AudioInfo)
        return StepOutcome()

    def transcript_step(store: JobStore) -> StepOutcome:
        if kind == "texto":
            store.write("transcript.json", load_text(input_path), Transcript)
            return StepOutcome()
        audio = store.read("audio.json", AudioInfo)
        if not Path(audio.path).exists():
            raise FileNotFoundError(
                f"Falta {audio.path} (¿se usó --purge-audio?); repite desde --from-step audio"
            )
        transcript = deps.transcriber.transcribe(audio)
        store.write("transcript.json", transcript, Transcript)
        if purge_audio_after:
            purge_audio(audio)
        seconds = transcript.duration or audio.duration
        return StepOutcome(cost=[transcription_cost("transcript", transcript.provider, seconds)])

    def speakers_step(store: JobStore) -> StepOutcome:
        def work() -> None:
            info_path = store.path(SPEAKER_INFO_FILE)
            info = (
                store.read(SPEAKER_INFO_FILE, dict[str, SpeakerInfo]) if info_path.exists() else {}
            )
            speakers = identify_speakers(
                store.read("transcript.json", Transcript),
                speech_date=store.manifest.speech_date,
                llm=deps.llm,
                model=deps.model_for("detection"),
                governments=default_governments(),
                info=info,
            )
            store.write("speakers.json", speakers, SpeakerMap)

        return _with_llm_costs(deps, work)

    def claims_step(store: JobStore) -> StepOutcome:
        def work() -> None:
            detection = detect_claims(
                store.read("transcript.json", Transcript),
                speech_date=store.manifest.speech_date,
                input_kind=kind,
                llm=deps.llm,
                model=deps.model_for("detection"),
            )
            store.write("claims.json", detection, ClaimDetection)

        return _with_llm_costs(deps, work)

    def context_step(store: JobStore) -> StepOutcome:
        def work() -> None:
            resolved = resolve_context(
                store.read("claims.json", ClaimDetection),
                store.read("transcript.json", Transcript),
                store.read("speakers.json", SpeakerMap),
                speech_date=store.manifest.speech_date,
                llm=deps.llm,
                # Interpreting who "ustedes" is needs the careful model.
                model=deps.model_for("verdict"),
                governments=default_governments(),
            )
            store.write("context.json", resolved, ClaimDetection)

        return _with_llm_costs(deps, work)

    def evidence_step(store: JobStore) -> StepOutcome:
        claims = store.read("context.json", ClaimDetection).claims
        # Unresolved claims are no_verificable anyway: don't spend searches on them.
        searchable = [c for c in claims if c.context_status != "no_resuelto"]
        found = (
            {
                e.claim_id: e
                for e in collect_evidence(
                    searchable, deps.evidence_sources, store.manifest.speech_date
                )
            }
            if searchable
            else {}
        )
        sources = deps.evidence_sources
        run = EvidenceRun(
            sources_used=[s.name for s in sources],
            sources_disabled=deps.disabled_sources,
            claims=[found.get(c.id, ClaimEvidence(claim_id=c.id)) for c in claims],
        )
        store.write("evidence.json", run, EvidenceRun)
        return StepOutcome()

    def verdicts_step(store: JobStore) -> StepOutcome:
        def work() -> None:
            verdicts = decide_verdicts(
                store.read("context.json", ClaimDetection).claims,
                store.read("evidence.json", EvidenceRun).claims,
                speech_date=store.manifest.speech_date,
                llm=deps.llm,
                model=deps.model_for("verdict"),
                follow_up=follow_up_search(deps, store.manifest.speech_date),
            )
            store.write("verdicts.json", verdicts, list[Verdict])

        return _with_llm_costs(deps, work)

    def report_step(store: JobStore) -> StepOutcome:
        evidence = store.read("evidence.json", EvidenceRun)
        profiles = store.read("speakers.json", SpeakerMap)
        display = {s.label: s.display() for s in profiles.speakers if s.name or s.party}
        for label, name in store.manifest.speakers.items():  # --oradores / --autor win
            known = profiles.get(label)
            party = known.party if known else None
            display[label] = f"{name} ({party})" if party else name
        report = build_report(
            job_id=store.job_id,
            source=input_path.name,
            input_kind=kind,
            speech_date=store.manifest.speech_date,
            speakers=display,
            speaker_profiles=profiles.speakers,
            detection=store.read("context.json", ClaimDetection),
            verdicts=store.read("verdicts.json", list[Verdict]),
            cost=store.manifest.run_cost(),
            generated_at=dt.datetime.now(dt.UTC),
            extra_warnings=[
                f"Fuente «{name}» desactivada: falta su clave en .env"
                for name in evidence.sources_disabled
            ],
        )
        store.write("report.json", report, Report)
        return StepOutcome()

    steps = [
        Step("transcript", transcript_step),
        Step("speakers", speakers_step, (SPEAKERS_PROMPT,)),
        Step("claims", claims_step, (CLAIMS_PROMPT,)),
        Step("context", context_step, (CONTEXT_PROMPT,)),
        Step("evidence", evidence_step),
        Step("verdicts", verdicts_step, (VERDICT_PROMPT,)),
        Step("report", report_step),
    ]
    if kind == "video":
        steps.insert(0, Step("audio", audio_step))
    return steps


def _store_speaker_info(store: JobStore, info: dict[str, SpeakerInfo] | None) -> bool:
    """Save the user's speaker data; True if it changed (speakers must be identified again).
    None keeps whatever was given before."""
    if info is None:
        return False
    path = store.path(SPEAKER_INFO_FILE)
    previous = store.read(SPEAKER_INFO_FILE, dict[str, SpeakerInfo]) if path.exists() else {}
    if previous == info:
        return False
    store.write(SPEAKER_INFO_FILE, info, dict[str, SpeakerInfo])
    return True


def run_job(
    input_path: Path,
    *,
    speech_date: dt.date,
    data_dir: Path,
    deps: Deps,
    prompts_dir: Path = PROMPTS_DIR,
    speakers: dict[str, str] | None = None,
    speaker_info: dict[str, SpeakerInfo] | None = None,
    author: str | None = None,
    from_step: StepName | None = None,
    purge_audio: bool = False,
) -> JobResult:
    if not input_path.is_file():
        raise FileNotFoundError(f"No existe el archivo {input_path}")
    kind = input_kind_for(input_path)
    if author is not None:
        if kind != "texto":
            raise ValueError("--autor solo se usa con texto; en vídeo usa --oradores")
        speakers = {**(speakers or {}), TEXT_SPEAKER: author}

    store, invalidated = JobStore.open_or_create(data_dir, input_path, kind, speech_date, speakers)
    if _store_speaker_info(store, speaker_info):
        invalidated = earliest_step(invalidated, "speakers")
    start = earliest_step(from_step, invalidated)
    if invalidated:
        logger.info("Parámetros cambiados: se recalcula desde %s", invalidated)
    steps = build_steps(store, input_path, deps, purge_audio_after=purge_audio)
    steps_run = run_steps(store, steps, prompts_dir, start)

    report = store.read("report.json", Report)
    report_path = store.path("report.json")
    return JobResult(
        report=report,
        report_path=report_path,
        summary=format_summary(report, report_path=str(report_path)),
        steps_run=steps_run,
    )
