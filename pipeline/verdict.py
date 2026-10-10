"""Veredicto basado solo en la evidencia recuperada."""

import datetime as dt
import html
import logging
from collections.abc import Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator

from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import ClaimEvidence, DetectedClaim, EvidenceItem, Verdict, VerdictLabel
from pipeline.neutrality import FORBIDDEN_WORDS_RE

logger = logging.getLogger(__name__)

PROMPT = "veredicto.md"
# CLAUDE.md: a verdict resting only on general web sources carries less confidence.
WEB_ONLY_MAX_CONFIDENCE = 0.6
# A second, targeted search round when the first evidence cannot settle the claim.
MAX_FOLLOW_UP_QUERIES = 3

# (claim, queries, first evidence number) -> (new items numbered from it, errors)
FollowUpSearch = Callable[[DetectedClaim, list[str], int], tuple[list[EvidenceItem], list[str]]]

NO_EVIDENCE = "No se ha encontrado evidencia sobre esta afirmación en las fuentes consultadas."
SEARCH_FAILED = (
    "La búsqueda de evidencia falló para esta afirmación, así que no se ha podido verificar."
)
NO_VALID_CITATION = (
    " (Se ha clasificado como no verificable porque el análisis concluyó sin citar evidencia "
    "válida.)"
)

T = TypeVar("T", bound=BaseModel)


class VerdictError(RuntimeError):
    """The verdict step could not produce a usable result."""


class StructuredLLM(Protocol):
    def structured(
        self, *, step: str, model: str, prompt: str, user_content: str, output_type: type[T]
    ) -> T: ...


# Schema of Claude's answer (see prompts/veredicto.md).
class VerdictAnswer(BaseModel):
    verdict: VerdictLabel
    confidence: float = Field(ge=0, le=1)
    explanation: str
    cited_evidence: list[str]
    matched_factcheck: str | None
    # Only with no_verificable: searches for the specific data that would settle it.
    follow_up_queries: list[str]

    @field_validator("explanation")
    @classmethod
    def _neutral_wording(cls, v: str) -> str:
        # Rejected here (not only in Verdict) so LLMClient retries the call.
        if FORBIDDEN_WORDS_RE.search(v):
            raise ValueError("explanation uses forbidden wording")
        return v


def _evidence_block(item: EvidenceItem) -> str:
    source = item.source
    attrs = {
        "id": item.id,
        "tipo": source.kind,
        "organismo": source.organization,
        "fecha": source.date.isoformat() if source.date else "desconocida",
        "url": source.url,
    }
    attr_text = " ".join(f'{k}="{html.escape(v, quote=True)}"' for k, v in attrs.items())
    lines = [f"título: {source.title}"]
    if item.claim_reviewed:
        lines.append(f"afirmación revisada por el verificador: {item.claim_reviewed}")
    if item.rating:
        lines.append(f"valoración del verificador: {item.rating}")
    lines.append(f"extracto: {item.excerpt}")
    body = html.escape("\n".join(lines), quote=False)
    return f"<evidencia {attr_text}>\n{body}\n</evidencia>"


def format_claim(
    claim: DetectedClaim, evidence: ClaimEvidence, speech_date: dt.date, final: bool = False
) -> str:
    """User message for one claim. The speaker is left out on purpose (neutrality)."""
    parts = [
        f"<fecha>{speech_date.isoformat()}</fecha>",
        f"<afirmacion>{html.escape(claim.text_normalized, quote=False)}</afirmacion>",
        f"<cita_original>{html.escape(claim.text_original, quote=False)}</cita_original>",
        f"<tipo>{claim.claim_type}</tipo>",
        *(_evidence_block(item) for item in evidence.items),
    ]
    if final:
        parts.append(
            "<ronda_final>No habrá más búsquedas: decide con esta evidencia.</ronda_final>"
        )
    return "\n".join(parts)


def _no_evidence(claim: DetectedClaim, evidence: ClaimEvidence) -> Verdict:
    return Verdict(
        claim_id=claim.id,
        verdict="no_verificable",
        confidence=0.0,
        explanation=SEARCH_FAILED if evidence.errors else NO_EVIDENCE,
        evidence_errors=evidence.errors,
    )


def _failed(claim: DetectedClaim, evidence: ClaimEvidence, error: str) -> Verdict:
    return Verdict(
        claim_id=claim.id,
        verdict="no_verificable",
        confidence=0.0,
        explanation="No se pudo completar el análisis de esta afirmación.",
        analysis_error=error,
        evidence_errors=evidence.errors,
    )


def _apply_rules(
    claim: DetectedClaim,
    evidence: ClaimEvidence,
    answer: VerdictAnswer,
    follow_up_queries: list[str] | None = None,
) -> Verdict:
    """Turn Claude's answer into a Verdict that respects CLAUDE.md's principles."""
    by_id = {item.id: item for item in evidence.items}
    cited = [by_id[i] for i in dict.fromkeys(answer.cited_evidence) if i in by_id]
    unknown = [i for i in answer.cited_evidence if i not in by_id]
    if unknown:
        logger.warning("%s cita evidencia inexistente: %s", claim.id, unknown)

    verdict, confidence, explanation = answer.verdict, answer.confidence, answer.explanation
    if verdict != "no_verificable" and not cited:
        verdict, explanation = "no_verificable", explanation + NO_VALID_CITATION
    if verdict == "no_verificable":
        confidence = 0.0
    elif all(item.source.kind == "web" for item in cited):
        confidence = min(confidence, WEB_ONLY_MAX_CONFIDENCE)

    matched = None
    if answer.matched_factcheck in {i.id for i in cited}:
        item = by_id[answer.matched_factcheck]
        if item.source.kind == "factcheck":
            matched = item.source.url

    return Verdict(
        claim_id=claim.id,
        verdict=verdict,
        confidence=confidence,
        explanation=explanation,
        sources=[item.source for item in cited],
        matched_factcheck=matched,
        evidence_errors=evidence.errors,
        follow_up_queries=follow_up_queries or [],
    )


def _decide(
    claim: DetectedClaim,
    evidence: ClaimEvidence,
    speech_date: dt.date,
    llm: StructuredLLM,
    model: str,
    follow_up: FollowUpSearch | None,
) -> Verdict:
    def ask(ev: ClaimEvidence, final: bool) -> VerdictAnswer:
        return llm.structured(
            step="verdicts",
            model=model,
            prompt=PROMPT,
            user_content=format_claim(claim, ev, speech_date, final=final),
            output_type=VerdictAnswer,
        )

    answer = ask(evidence, final=follow_up is None)
    queries = [q.strip() for q in answer.follow_up_queries if q.strip()][:MAX_FOLLOW_UP_QUERIES]
    if answer.verdict != "no_verificable" or not queries or follow_up is None:
        return _apply_rules(claim, evidence, answer)

    logger.info("%s: búsqueda adicional (%s)", claim.id, "; ".join(queries))
    items, errors = follow_up(claim, queries, len(evidence.items) + 1)
    known = {i.source.url for i in evidence.items}
    items = [i for i in items if i.source.url not in known]
    extended = evidence.model_copy(
        update={"items": [*evidence.items, *items], "errors": [*evidence.errors, *errors]}
    )
    if not items:
        return _apply_rules(claim, extended, answer, queries)
    return _apply_rules(claim, extended, ask(extended, final=True), queries)


def decide_verdicts(
    claims: list[DetectedClaim],
    evidences: list[ClaimEvidence],
    *,
    speech_date: dt.date,
    llm: StructuredLLM,
    model: str,
    follow_up: FollowUpSearch | None = None,
) -> list[Verdict]:
    """One verdict per claim, based only on that claim's retrieved evidence.

    With `follow_up`, a claim Claude cannot settle gets one extra targeted search for
    the data it says is missing, and a final decision with the extended evidence.

    Claims without evidence are `no_verificable` without calling Claude. A failed
    analysis is recorded in `analysis_error`; a config error, or every analysis
    failing, stops the step.
    """
    by_claim = {e.claim_id: e for e in evidences}
    verdicts: list[Verdict] = []
    attempted = failed = 0

    for claim in claims:
        evidence = by_claim.get(claim.id)
        if evidence is None:
            raise VerdictError(f"Falta la evidencia de la afirmación {claim.id}")
        if not evidence.items:
            verdicts.append(_no_evidence(claim, evidence))
            continue

        attempted += 1
        logger.info("Veredicto de %s (%d/%d)", claim.id, len(verdicts) + 1, len(claims))
        try:
            verdicts.append(_decide(claim, evidence, speech_date, llm, model, follow_up))
        except LLMConfigError:
            raise
        except (LLMError, ValidationError) as exc:
            logger.warning("Veredicto de %s falló: %s", claim.id, exc)
            failed += 1
            verdicts.append(_failed(claim, evidence, str(exc)))

    if attempted and failed == attempted:
        first = next(v.analysis_error for v in verdicts if v.analysis_error)
        raise VerdictError(f"Todos los veredictos fallaron: {first}")
    return verdicts
