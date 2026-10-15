"""Modelos pydantic compartidos por todos los pasos del pipeline."""

import datetime as dt
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from pipeline.neutrality import FORBIDDEN_WORDS_RE

ClaimType = Literal["estadistica", "hecho_historico", "legal", "comparacion", "otro"]
VerdictLabel = Literal["verdadero", "verdad_a_medias", "confusion", "falso", "no_verificable"]
# "indicador_local": official series kept in data/knowledge/indicators (no web search).
SourceKind = Literal["factcheck", "oficial", "indicador_local", "web"]
# "promesa": a concrete commitment about the future; assessed for feasibility, not truth.
ClaimKind = Literal["afirmacion", "promesa"]
PromiseLabel = Literal["viable", "dudosa", "inviable", "no_evaluable"]
PROMISE_LABELS: tuple[PromiseLabel, ...] = ("viable", "dudosa", "inviable", "no_evaluable")
ObstacleKind = Literal["legal", "competencias", "presupuesto", "datos", "otro"]
InputKind = Literal["video", "texto"]

VERDICT_LABELS: tuple[VerdictLabel, ...] = (
    "verdadero",
    "verdad_a_medias",
    "confusion",
    "falso",
    "no_verificable",
)

# What the user sees: the verdict plus whether a reliable source backs it.
Category = Literal[
    "verdadero",
    "verdadero_sin_fuente_oficial",
    "verdad_a_medias",
    "confusion",
    "no_verificable",
    "falso_sin_fuente_oficial",
    "falso_con_fuente_oficial",
]
CATEGORIES: tuple[Category, ...] = (
    "verdadero",
    "verdadero_sin_fuente_oficial",
    "verdad_a_medias",
    "confusion",
    "no_verificable",
    "falso_sin_fuente_oficial",
    "falso_con_fuente_oficial",
)
# Public bodies and professional fact-checkers count as "fuente oficial".
RELIABLE_KINDS: frozenset[str] = frozenset({"oficial", "factcheck", "indicador_local"})


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

# "oficial"/"web": found by the speaker research step (see `references`).
SpeakerSource = Literal["archivo", "texto", "tabla_gobiernos", "base_politicos", "oficial", "web"]


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
    # Pages that support the research step's findings.
    references: list["Source"] = Field(default_factory=list)

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
    kind: ClaimKind = "afirmacion"
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
    # Repetition of an earlier claim of the same speaker: shares its verdict (see duplicates.py).
    duplicate_of: str | None = None

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
    # Official indicator of the local base the claim is about (data/knowledge/indicators) and
    # the years it refers to: when covered, the evidence comes from there, not the web.
    indicator: str | None = None
    years: list[int] = Field(default_factory=list)


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


# Speaker.references is declared before Source.
Speaker.model_rebuild()
SpeakerMap.model_rebuild()


class EvidenceItem(BaseModel):
    id: str  # short id the verdict prompt uses to cite it, e.g. "E1"
    source: Source
    excerpt: str
    # ClaimReview-specific fields; empty for other source kinds.
    claim_reviewed: str | None = None
    rating: str | None = None
    # Rows of ref.observations behind a local series excerpt (traceability, `impacto`).
    observation_ids: list[int] = Field(default_factory=list)


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
    # Problems with the local base (e.g. knowledge base unreachable: JSON base used).
    warnings: list[str] = Field(default_factory=list)
    claims: list[ClaimEvidence] = Field(default_factory=list)


# --- Verdicts ---------------------------------------------------------------


Stance = Literal["apoya", "contradice", "neutral"]


class EvidenceAssessment(BaseModel):
    id: str  # "E3"
    says: str  # what that source states about the claim, briefly
    stance: Stance


class VerdictAnalysis(BaseModel):
    """Auditable analysis the verdict rests on (verdicts.json only, never shown in the UI).

    Named "analysis" rather than "reasoning": Claude 5.5 models refuse requests that ask
    them to write out their internal reasoning in the response (reasoning_extraction)."""

    claim_parsed: str  # figure, unit, period and actor of the claim
    evidence_assessment: list[EvidenceAssessment]
    numeric_comparison: str
    nuances: str  # nuances found and whether they change the conclusion, and why
    conclusion: str


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
    # Set when the checks after the verdict failed twice (invented source ids, a label that
    # contradicts its own explanation…): the verdict is then no_verificable until reviewed.
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def basis(self) -> SourceKind | None:
        """Strongest kind of source the verdict rests on: factcheck > oficial > web."""
        kinds = {s.kind for s in self.sources}
        return next(
            (k for k in ("factcheck", "oficial", "indicador_local", "web") if k in kinds), None
        )

    @computed_field
    @property
    def category(self) -> Category:
        if self.verdict in ("no_verificable", "confusion", "verdad_a_medias"):
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
    # Audit trail kept in verdicts.json and left out of the report.
    evidence_used: list[str] = Field(default_factory=list)
    analysis: VerdictAnalysis | None = None
    numeric_check: str = ""
    thinking_summary: str | None = None  # API summary of Claude's thinking, when enabled
    attempts: int = 1

    AUDIT_FIELDS: ClassVar[set[str]] = {
        "evidence_used",
        "analysis",
        "numeric_check",
        "thinking_summary",
        "attempts",
    }


class Claim(ClaimBase, VerdictFields):
    """Final, verified claim as it appears in the report."""


class PromiseFields(BaseModel):
    """Feasibility of a promise, based only on the retrieved evidence."""

    assessment: PromiseLabel
    confidence: float = Field(ge=0, le=1)
    explanation: str  # why it is (not) feasible: legal framework, data, budget
    obstacles: list[ObstacleKind] = Field(default_factory=list)
    # Consequences stated by the cited sources ("" if they state none).
    consequences_sourced: str = ""
    # The model's own brief reasoning, always labelled as such ("" if none).
    consequences_reasoning: str = ""
    sources: list[Source] = Field(default_factory=list)
    analysis_error: str | None = None
    evidence_errors: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def basis(self) -> SourceKind | None:
        kinds = {s.kind for s in self.sources}
        return next(
            (k for k in ("factcheck", "oficial", "indicador_local", "web") if k in kinds), None
        )

    @field_validator("explanation", "consequences_sourced", "consequences_reasoning")
    @classmethod
    def _neutral_wording(cls, v: str) -> str:
        if FORBIDDEN_WORDS_RE.search(v):
            raise ValueError("text uses forbidden wording")
        return v

    @model_validator(mode="after")
    def _consistent_sources(self) -> Self:
        if self.assessment != "no_evaluable" and not self.sources:
            raise ValueError("an assessment other than no_evaluable needs at least one source")
        return self


class PromiseVerdict(PromiseFields):
    claim_id: str


class Promise(ClaimBase, PromiseFields):
    """Final, assessed promise as it appears in the report."""


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
    # Claude: wall time of the call and, with thinking, how many output tokens were thinking.
    seconds: float = 0
    thinking_tokens: int = 0
    # Web searches (Tavily): paid ones, ones served from the disk cache, credits spent.
    searches: int = 0
    cached_searches: int = 0
    credits: int = 0
    # Claims answered by the local base of indicators instead of a web search.
    indicator_hits: int = 0
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
    # Shown in their own section; not counted in the claim totals.
    promises: list[Promise] = Field(default_factory=list)
    cost: RunCost
    # Seconds per step of the run that produced each step's output (manifest).
    step_seconds: dict[str, float] = Field(default_factory=dict)
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
        counts = {"factcheck": 0, "oficial": 0, "indicador_local": 0, "web": 0, "sin_fuente": 0}
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
    def by_promise(self) -> dict[str, int]:
        counts = dict.fromkeys(PROMISE_LABELS, 0)
        for p in self.promises:
            counts[p.assessment] += 1
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
    "audio",
    "transcript",
    "speakers",
    "speaker_research",
    "claims",
    "context",
    "evidence",
    "verdicts",
    "promises",
    "report",
]
STEPS: tuple[StepName, ...] = (
    "audio",
    "transcript",
    "speakers",
    "speaker_research",
    "claims",
    "context",
    "evidence",
    "verdicts",
    "promises",
    "report",
)


class VerdictRecord(BaseModel):
    """Whether the verdicts were stored in the knowledge base (verif.claims)."""

    stored: int = 0
    error: str | None = None


class StepRecord(BaseModel):
    # "partial": finished, but some items failed (e.g. usage limit); retried next run.
    status: Literal["done", "partial", "failed"]
    finished_at: dt.datetime
    # Prompt file name -> content hash used by this run of the step.
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    cost: list[CostEntry] = Field(default_factory=list)
    error: str | None = None
    # How long the step took (fase I metrics); None in manifests written before.
    seconds: float | None = None


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
