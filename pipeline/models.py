"""Modelos pydantic compartidos por todos los pasos del pipeline."""

import datetime as dt
import re
from typing import Literal, Self

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

ClaimType = Literal["estadistica", "hecho_historico", "legal", "comparacion", "otro"]
VerdictLabel = Literal["verdadero", "enganoso", "falso", "no_verificable"]
SourceKind = Literal["factcheck", "oficial", "web"]

VERDICT_LABELS: tuple[VerdictLabel, ...] = ("verdadero", "enganoso", "falso", "no_verificable")

# Vocabulary that judges intent instead of facts; banned from outputs, prompts and UI.
FORBIDDEN_WORDS_RE = re.compile(
    r"\b(mentira|mentiras|miente|mienten|mintió|mintieron|mentiroso|mentirosa|mentirosos|mentirosas)\b",
    re.IGNORECASE,
)


def _check_time_range(start: float, end: float) -> None:
    if start < 0:
        raise ValueError("start must be >= 0")
    if end < start:
        raise ValueError("end must be >= start")


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
    """A contiguous stretch of speech by a single diarized speaker."""

    speaker: str
    start: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_range(self) -> Self:
        _check_time_range(self.start, self.end)
        return self


class Transcript(BaseModel):
    provider: str
    language: str
    duration: float = Field(ge=0)
    segments: list[Segment]

    @property
    def speakers(self) -> list[str]:
        return sorted({s.speaker for s in self.segments})


# --- Claims -----------------------------------------------------------------


class ClaimBase(BaseModel):
    id: str
    speaker: str
    start: float
    end: float
    text_original: str = Field(min_length=1)
    text_normalized: str = Field(min_length=1)
    claim_type: ClaimType

    @model_validator(mode="after")
    def _valid_range(self) -> Self:
        _check_time_range(self.start, self.end)
        return self


class DetectedClaim(ClaimBase):
    """Claim found in the transcript, before looking for evidence."""

    # Short keyword queries for search-based sources; internal, not shown in the report.
    search_queries: list[str] = Field(default_factory=list)


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


# --- Verdicts ---------------------------------------------------------------


class VerdictFields(BaseModel):
    verdict: VerdictLabel
    confidence: float = Field(ge=0, le=1)
    explanation: str
    sources: list[Source] = Field(default_factory=list)
    matched_factcheck: str | None = None

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


class RunCost(BaseModel):
    entries: list[CostEntry] = Field(default_factory=list)

    @computed_field
    @property
    def total_usd(self) -> float:
        return round(sum(e.usd for e in self.entries), 6)


class Report(BaseModel):
    job_id: str
    video: str
    speech_date: dt.date
    generated_at: dt.datetime
    # Diarized label -> real name; labels without a mapping are shown as-is.
    speakers: dict[str, str] = Field(default_factory=dict)
    claims: list[Claim]
    cost: RunCost

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
    def by_speaker(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for c in self.claims:
            counts = result.setdefault(
                self.speaker_name(c.speaker), dict.fromkeys(VERDICT_LABELS, 0)
            )
            counts[c.verdict] += 1
        return result


# --- Jobs -------------------------------------------------------------------

StepName = Literal["audio", "transcript", "claims", "evidence", "verdicts", "report"]
STEPS: tuple[StepName, ...] = ("audio", "transcript", "claims", "evidence", "verdicts", "report")


class StepRecord(BaseModel):
    status: Literal["done", "failed"]
    finished_at: dt.datetime
    # Prompt file name -> content hash used by this run of the step.
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    cost: list[CostEntry] = Field(default_factory=list)
    error: str | None = None


class JobManifest(BaseModel):
    job_id: str
    video_path: str
    video_sha256: str
    speech_date: dt.date
    speakers: dict[str, str] = Field(default_factory=dict)
    created_at: dt.datetime
    steps: dict[StepName, StepRecord] = Field(default_factory=dict)

    def run_cost(self) -> RunCost:
        """Cost of the step runs whose outputs make up the current report."""
        return RunCost(entries=[e for s in STEPS if s in self.steps for e in self.steps[s].cost])
