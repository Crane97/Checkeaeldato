"""Modelos pydantic compartidos por todos los pasos del pipeline."""

import datetime as dt
from typing import Literal, Self

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from pipeline.neutrality import FORBIDDEN_WORDS_RE

ClaimType = Literal["estadistica", "hecho_historico", "legal", "comparacion", "otro"]
VerdictLabel = Literal["verdadero", "confusion", "falso", "no_verificable"]
SourceKind = Literal["factcheck", "oficial", "web"]
InputKind = Literal["video", "texto"]

VERDICT_LABELS: tuple[VerdictLabel, ...] = ("verdadero", "confusion", "falso", "no_verificable")

# What the user sees: the verdict plus whether a reliable source backs it.
Category = Literal[
    "verdadero",
    "verdadero_sin_fuente_oficial",
    "confusion",
    "no_verificable",
    "falso_sin_fuente_oficial",
    "falso_con_fuente_oficial",
]
CATEGORIES: tuple[Category, ...] = (
    "verdadero",
    "verdadero_sin_fuente_oficial",
    "confusion",
    "no_verificable",
    "falso_sin_fuente_oficial",
    "falso_con_fuente_oficial",
)
# Public bodies and professional fact-checkers count as "fuente oficial".
RELIABLE_KINDS: frozenset[str] = frozenset({"oficial", "factcheck"})


def _check_time_range(start: float | None, end: float | None) -> None:
    """Seconds in the video; both absent for text input, never only one."""
    if start is None or end is None:
        if start is not None or end is not None:
            raise ValueError("start and end must be both set (video) or both empty (text)")
        return
    if start < 0:
        raise ValueError("start must be >= 0")
    if end < start:
        raise ValueError("end must be >= start")


class DateRange(BaseModel):
    start: dt.date
    end: dt.date

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("end must be >= start")
        return self

    def label(self) -> str:
        return f"{self.start.strftime('%d/%m/%Y')}–{self.end.strftime('%d/%m/%Y')}"


# --- Audio ------------------------------------------------------------------


class AudioInfo(BaseModel):
    path: str
    duration: float = Field(ge=0)
    sample_rate: int


# --- Transcription -----------------------------------------------------------


class Word(BaseModel):
    text: str
    start: float
    end: float
    speaker: str | None = None

    @model_validator(mode="after")
    def _valid_range(self) -> Self:
        _check_time_range(self.start, self.end)
        return self


class Segment(BaseModel):
    """A stretch of speech by one diarized speaker, or a paragraph of text input."""

    speaker: str
    text: str
    start: float | None = None
    end: float | None = None
    words: list[Word] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_range(self) -> Self:
        _check_time_range(self.start, self.end)
        return self


class Transcript(BaseModel):
    provider: str  # transcription provider, or "texto" for text input
    language: str
    duration: float | None = Field(default=None, ge=0)  # seconds; None for text
    segments: list[Segment]

    @property
    def speakers(self) -> list[str]:
        return sorted({s.speaker for s in self.segments})


# --- Speakers ---------------------------------------------------------------

SpeakerSource = Literal["archivo", "texto", "tabla_gobiernos"]


class SpeakerInfo(BaseModel):
    """Identity given by the user (speakers.json or the web form)."""

    name: str | None = None
    party: str | None = None
    role: str | None = None


class Speaker(BaseModel):
    """Who a transcript label is, with the evidence for it. Unknown fields stay None."""

    label: str
    name: str | None = None
    party: str | None = None
    role: str | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    # Literal quotes from the transcript (or "speakers.json") supporting the identification.
    evidence: list[str] = Field(default_factory=list)
    sources: list[SpeakerSource] = Field(default_factory=list)

    def display(self) -> str:
        base = self.name or self.label
        return f"{base} ({self.party})" if self.party else base


class SpeakerMap(BaseModel):
    """Output of the speakers step (speakers.json)."""

    speakers: list[Speaker] = Field(default_factory=list)
    # Model calls that failed (identification incomplete; retried on the next run).
    errors: list[str] = Field(default_factory=list)

    def get(self, label: str) -> Speaker | None:
        return next((s for s in self.speakers if s.label == label), None)


# --- Claims -----------------------------------------------------------------


ContextStatus = Literal["no_necesario", "resuelto", "no_resuelto"]


class ClaimBase(BaseModel):
    id: str
    speaker: str
    segment: int | None = None  # index of the transcript segment where the quote starts
    # Literal quote from the transcript or text; must be findable in the source.
    text_original: str = Field(min_length=1)
    text_normalized: str = Field(min_length=1)
    claim_type: ClaimType
    # Seconds in the video; None for text input.
    start: float | None = None
    end: float | None = None
    # Context resolution ("ustedes", "este Gobierno"…): who and when the claim is about.
    context_status: ContextStatus = "no_necesario"
    resolved_actor: str | None = None
    time_scope: list[DateRange] = Field(default_factory=list)
    resolution_confidence: float | None = None
    resolution_notes: str | None = None

    @model_validator(mode="after")
    def _valid_range(self) -> Self:
        _check_time_range(self.start, self.end)
        return self


class DetectedClaim(ClaimBase):
    """Claim found in the transcript, before looking for evidence."""

    # Short keyword queries for search-based sources; internal, not shown in the report.
    search_queries: list[str] = Field(default_factory=list)
    # Has implicit actors or periods that the context step must resolve.
    needs_context: bool = False


class RejectedClaim(BaseModel):
    """A claim proposed by the model but discarded, kept for traceability."""

    quote: str
    segment: int
    reason: str


class BlockFailure(BaseModel):
    block: int
    segments: list[int]
    error: str


class ClaimDetection(BaseModel):
    """Output of the claims step (claims.json)."""

    claims: list[DetectedClaim] = Field(default_factory=list)
    rejected: list[RejectedClaim] = Field(default_factory=list)
    failed_blocks: list[BlockFailure] = Field(default_factory=list)
    # Context resolutions that failed for technical reasons (retried on the next run).
    context_errors: list[str] = Field(default_factory=list)


# --- Evidence ---------------------------------------------------------------


class Source(BaseModel):
    url: str
    title: str
    organization: str
    date: dt.date | None = None
    kind: SourceKind

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        return v


class EvidenceItem(BaseModel):
    id: str  # short id the verdict prompt uses to cite it, e.g. "E1"
    source: Source
    excerpt: str
    # ClaimReview-specific fields; empty for other source kinds.
    claim_reviewed: str | None = None
    rating: str | None = None


class ClaimEvidence(BaseModel):
    claim_id: str
    items: list[EvidenceItem] = Field(default_factory=list)
    # Non-fatal failures from individual sources, kept for traceability.
    errors: list[str] = Field(default_factory=list)


class EvidenceRun(BaseModel):
    """Output of the evidence step (evidence.json)."""

    sources_used: list[str]
    # Sources skipped because their API key is missing, reported as a warning.
    sources_disabled: list[str] = Field(default_factory=list)
    claims: list[ClaimEvidence] = Field(default_factory=list)


# --- Verdicts ---------------------------------------------------------------


class VerdictFields(BaseModel):
    verdict: VerdictLabel
    confidence: float = Field(ge=0, le=1)
    explanation: str
    sources: list[Source] = Field(default_factory=list)
    matched_factcheck: str | None = None
    # Set when the analysis itself failed, so it is not mistaken for "no evidence".
    analysis_error: str | None = None
    # Evidence searches that failed for this claim (the evidence may be incomplete).
    evidence_errors: list[str] = Field(default_factory=list)
    # Extra searches requested when the first evidence could not settle the claim.
    follow_up_queries: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def basis(self) -> SourceKind | None:
        """Strongest kind of source the verdict rests on: factcheck > oficial > web."""
        kinds = {s.kind for s in self.sources}
        return next((k for k in ("factcheck", "oficial", "web") if k in kinds), None)

    @computed_field
    @property
    def category(self) -> Category:
        if self.verdict in ("no_verificable", "confusion"):
            return self.verdict
        reliable = any(s.kind in RELIABLE_KINDS for s in self.sources)
        if self.verdict == "verdadero":
            return "verdadero" if reliable else "verdadero_sin_fuente_oficial"
        return "falso_con_fuente_oficial" if reliable else "falso_sin_fuente_oficial"

    @field_validator("explanation")
    @classmethod
    def _neutral_wording(cls, v: str) -> str:
        if FORBIDDEN_WORDS_RE.search(v):
            raise ValueError("explanation uses forbidden wording")
        return v

    @model_validator(mode="after")
    def _consistent_sources(self) -> Self:
        if self.verdict != "no_verificable" and not self.sources:
            raise ValueError("a verdict other than no_verificable needs at least one source")
        if self.matched_factcheck is not None:
            factcheck_urls = {s.url for s in self.sources if s.kind == "factcheck"}
            if self.matched_factcheck not in factcheck_urls:
                raise ValueError("matched_factcheck must be the url of a cited factcheck source")
        return self


class Verdict(VerdictFields):
    claim_id: str


class Claim(ClaimBase, VerdictFields):
    """Final, verified claim as it appears in the report."""


# --- Cost and report --------------------------------------------------------


class CostEntry(BaseModel):
    step: str
    provider: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    audio_seconds: float = 0
    usd: float = Field(ge=0)
    # False when the model/provider is missing from pricing.toml (usd is then 0).
    priced: bool = True


class RunCost(BaseModel):
    entries: list[CostEntry] = Field(default_factory=list)

    @computed_field
    @property
    def total_usd(self) -> float:
        return round(sum(e.usd for e in self.entries), 6)


class Report(BaseModel):
    job_id: str
    source: str  # input file name
    input_kind: InputKind
    speech_date: dt.date
    generated_at: dt.datetime
    # Diarized label -> display name ("Ana Ruiz (PSOE)"); unmapped labels shown as-is.
    speakers: dict[str, str] = Field(default_factory=dict)
    # Who each speaker is and on what evidence (speakers step).
    speaker_profiles: list[Speaker] = Field(default_factory=list)
    claims: list[Claim]
    cost: RunCost
    # Problems that make the report less complete (failed blocks, rejected quotes…).
    warnings: list[str] = Field(default_factory=list)

    def speaker_name(self, label: str) -> str:
        return self.speakers.get(label, label)

    @computed_field
    @property
    def by_verdict(self) -> dict[str, int]:
        counts = dict.fromkeys(VERDICT_LABELS, 0)
        for c in self.claims:
            counts[c.verdict] += 1
        return counts

    @computed_field
    @property
    def by_basis(self) -> dict[str, int]:
        counts = {"factcheck": 0, "oficial": 0, "web": 0, "sin_fuente": 0}
        for c in self.claims:
            counts[c.basis or "sin_fuente"] += 1
        return counts

    @computed_field
    @property
    def by_category(self) -> dict[str, int]:
        counts = dict.fromkeys(CATEGORIES, 0)
        for c in self.claims:
            counts[c.category] += 1
        return counts

    @computed_field
    @property
    def by_speaker(self) -> dict[str, dict[str, int]]:
        """Counts per speaker and category."""
        result: dict[str, dict[str, int]] = {}
        for c in self.claims:
            counts = result.setdefault(self.speaker_name(c.speaker), dict.fromkeys(CATEGORIES, 0))
            counts[c.category] += 1
        return result


# --- Jobs -------------------------------------------------------------------

StepName = Literal[
    "audio", "transcript", "speakers", "claims", "context", "evidence", "verdicts", "report"
]
STEPS: tuple[StepName, ...] = (
    "audio",
    "transcript",
    "speakers",
    "claims",
    "context",
    "evidence",
    "verdicts",
    "report",
)


class StepRecord(BaseModel):
    # "partial": finished, but some items failed (e.g. usage limit); retried next run.
    status: Literal["done", "partial", "failed"]
    finished_at: dt.datetime
    # Prompt file name -> content hash used by this run of the step.
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    cost: list[CostEntry] = Field(default_factory=list)
    error: str | None = None


class JobManifest(BaseModel):
    job_id: str
    input_path: str
    input_sha256: str
    input_kind: InputKind
    speech_date: dt.date
    speakers: dict[str, str] = Field(default_factory=dict)
    created_at: dt.datetime
    steps: dict[StepName, StepRecord] = Field(default_factory=dict)

    def run_cost(self) -> RunCost:
        """Cost of the step runs whose outputs make up the current report."""
        return RunCost(entries=[e for s in STEPS if s in self.steps for e in self.steps[s].cost])
