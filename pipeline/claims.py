"""Detección y normalización de afirmaciones verificables con Claude."""

import datetime as dt
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import (
    BlockFailure,
    ClaimDetection,
    ClaimType,
    DetectedClaim,
    InputKind,
    RejectedClaim,
    Segment,
    Transcript,
)

logger = logging.getLogger(__name__)

PROMPT = "deteccion_afirmaciones.md"
# ~1,500 tokens of new text per call, plus context: a 2 h debate is ~20 calls.
CORE_CHARS = 6000
CONTEXT_CHARS = 1500
MAX_SEARCH_QUERIES = 4

T = TypeVar("T", bound=BaseModel)


class ClaimsError(RuntimeError):
    """The claims step could not produce a usable result."""


class StructuredLLM(Protocol):
    def structured(
        self, *, step: str, model: str, prompt: str, user_content: str, output_type: type[T]
    ) -> T: ...


# Schema of Claude's answer (see prompts/deteccion_afirmaciones.md).
class ExtractedClaim(BaseModel):
    segment: int
    quote: str
    text_normalized: str
    claim_type: ClaimType
    search_queries: list[str]


class DetectionResult(BaseModel):
    claims: list[ExtractedClaim]


@dataclass(frozen=True)
class Block:
    """Segment indices sent in one call; only `core` claims are kept from it."""

    index: int
    before: list[int]
    core: list[int]
    after: list[int]


def build_blocks(
    segments: list[Segment], core_chars: int = CORE_CHARS, context_chars: int = CONTEXT_CHARS
) -> list[Block]:
    """Split segments into consecutive cores of about `core_chars`, with context around.

    Every segment is in exactly one core. Context takes whole segments up to
    `context_chars`, but at least one neighbour when context is enabled, so
    references across the boundary ("esa cifra") can be resolved.
    """
    cores: list[list[int]] = []
    i = 0
    while i < len(segments):
        core, size = [i], len(segments[i].text)
        i += 1
        while i < len(segments) and size + len(segments[i].text) <= core_chars:
            size += len(segments[i].text)
            core.append(i)
            i += 1
        cores.append(core)

    def take(indices: range) -> list[int]:
        taken: list[int] = []
        size = 0
        for j in indices:
            size += len(segments[j].text)
            if taken and size > context_chars:
                break
            taken.append(j)
        return taken if context_chars > 0 else []

    return [
        Block(
            index=n,
            before=sorted(take(range(core[0] - 1, -1, -1))),
            core=core,
            after=take(range(core[-1] + 1, len(segments))),
        )
        for n, core in enumerate(cores)
    ]


def _timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _segment_line(index: int, segment: Segment) -> str:
    time = f" ({_timestamp(segment.start)})" if segment.start is not None else ""
    return f"[S{index}] {segment.speaker}{time}: {segment.text}"


def format_block(
    transcript: Transcript, block: Block, speech_date: dt.date, input_kind: InputKind
) -> str:
    """User message for one block: data in tags, instructions live in the prompt file."""
    parts = [
        f"<fecha>{speech_date.isoformat()}</fecha>",
        f"<tipo_entrada>{input_kind}</tipo_entrada>",
    ]
    for tag, indices in (
        ("contexto_anterior", block.before),
        ("fragmento", block.core),
        ("contexto_posterior", block.after),
    ):
        if indices:
            lines = "\n".join(_segment_line(i, transcript.segments[i]) for i in indices)
            parts.append(f"<{tag}>\n{lines}\n</{tag}>")
    return "\n".join(parts)


_QUOTE_CHARS = str.maketrans(
    {"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "–": "-", "—": "-"}
)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).translate(_QUOTE_CHARS).replace("…", "...")
    return re.sub(r"\s+", " ", text).strip().casefold()


def _locate_quote(segments: list[Segment], index: int, quote: str) -> tuple[int, int, int] | None:
    """(first segment, last segment, position) of the quote, or None if not literal.

    A quote may run into the next segment of the same speaker (a sentence split by
    the transcriber).
    """
    needle = _normalize(quote)
    if not needle:
        return None
    first = _normalize(segments[index].text)
    if (pos := first.find(needle)) >= 0:
        return index, index, pos
    nxt = index + 1
    if nxt < len(segments) and segments[nxt].speaker == segments[index].speaker:
        joined = f"{first} {_normalize(segments[nxt].text)}"
        if (pos := joined.find(needle)) >= 0:
            return index, nxt, pos
    return None


def detect_claims(
    transcript: Transcript,
    *,
    speech_date: dt.date,
    input_kind: InputKind,
    llm: StructuredLLM,
    model: str,
    core_chars: int = CORE_CHARS,
    context_chars: int = CONTEXT_CHARS,
) -> ClaimDetection:
    segments = transcript.segments
    blocks = build_blocks(segments, core_chars, context_chars)
    found: dict[tuple[int, str], tuple[int, DetectedClaim]] = {}
    rejected: list[RejectedClaim] = []
    failures: list[BlockFailure] = []

    for block in blocks:
        logger.info("Detectando afirmaciones: bloque %d/%d", block.index + 1, len(blocks))
        try:
            result = llm.structured(
                step="claims",
                model=model,
                prompt=PROMPT,
                user_content=format_block(transcript, block, speech_date, input_kind),
                output_type=DetectionResult,
            )
        except LLMConfigError:
            raise
        except LLMError as exc:
            logger.warning("Bloque %d de afirmaciones falló: %s", block.index, exc)
            failures.append(BlockFailure(block=block.index, segments=block.core, error=str(exc)))
            continue

        core = set(block.core)
        for item in result.claims:
            if not 0 <= item.segment < len(segments):
                rejected.append(_reject(item, "segmento inexistente"))
                continue
            if item.segment not in core:
                continue  # context segment: the neighbouring block owns it
            span = _locate_quote(segments, item.segment, item.quote)
            if span is None:
                rejected.append(_reject(item, "la cita no aparece literalmente en el segmento"))
                continue
            first, last, pos = span
            key = (first, _normalize(item.quote))
            if key in found:
                continue
            try:
                claim = DetectedClaim(
                    id="pending",
                    speaker=segments[first].speaker,
                    text_original=item.quote.strip(),
                    text_normalized=item.text_normalized.strip(),
                    claim_type=item.claim_type,
                    start=segments[first].start,
                    end=segments[last].end,
                    search_queries=[q.strip() for q in item.search_queries if q.strip()][
                        :MAX_SEARCH_QUERIES
                    ],
                )
            except ValidationError as exc:
                rejected.append(_reject(item, f"datos inválidos: {exc.errors()[0]['msg']}"))
                continue
            found[key] = (pos, claim)

    if blocks and len(failures) == len(blocks):
        raise ClaimsError(f"Todos los bloques de afirmaciones fallaron: {failures[0].error}")

    ordered = sorted(found.items(), key=lambda kv: (kv[0][0], kv[1][0]))
    claims = [
        claim.model_copy(update={"id": f"c{n:03d}"})
        for n, (_, (_, claim)) in enumerate(ordered, start=1)
    ]
    return ClaimDetection(claims=claims, rejected=rejected, failed_blocks=failures)


def _reject(item: ExtractedClaim, reason: str) -> RejectedClaim:
    logger.info("Afirmación descartada (%s): %s", reason, item.quote)
    return RejectedClaim(quote=item.quote, segment=item.segment, reason=reason)
