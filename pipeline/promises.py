"""Análisis de viabilidad de las promesas, basado solo en la evidencia recuperada.

Una promesa no es verdadera ni falsa: se valora si es viable (marco legal de España y de la
UE, competencias, presupuesto, datos históricos) y qué consecuencias señalan las fuentes.
Claude puede añadir un razonamiento breve propio, que se muestra siempre marcado como tal.
"""

import datetime as dt
import html
import logging

from pydantic import BaseModel, Field, ValidationError, field_validator

from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import (
    ClaimEvidence,
    DetectedClaim,
    ObstacleKind,
    PromiseLabel,
    PromiseVerdict,
)
from pipeline.neutrality import FORBIDDEN_WORDS_RE
from pipeline.verdict import (
    WEB_ONLY_MAX_CONFIDENCE,
    StructuredLLM,
    _evidence_block,
    _selected_items,
)

logger = logging.getLogger(__name__)

PROMPT = "promesa.md"
REASONING_PREFIX = "Razonamiento propio (no procede de las fuentes): "
NO_EVIDENCE = (
    "No se ha encontrado información en las fuentes consultadas para valorar esta promesa."
)
SEARCH_FAILED = (
    "La búsqueda de información falló para esta promesa, así que no se ha podido valorar."
)
NO_VALID_CITATION = (
    " (Se ha clasificado como no evaluable porque el análisis concluyó sin citar fuentes válidas.)"
)


class PromiseError(RuntimeError):
    """The promises step could not produce a usable result."""


# Schema of Claude's answer (see prompts/promesa.md).
class PromiseAnswer(BaseModel):
    assessment: PromiseLabel
    confidence: float = Field(ge=0, le=1)
    explanation: str
    obstacles: list[ObstacleKind]
    consequences_sourced: str
    consequences_reasoning: str
    cited_evidence: list[str]

    @field_validator("explanation", "consequences_sourced", "consequences_reasoning")
    @classmethod
    def _neutral_wording(cls, v: str) -> str:
        if FORBIDDEN_WORDS_RE.search(v):
            raise ValueError("text uses forbidden wording")
        return v


def format_promise(claim: DetectedClaim, evidence: ClaimEvidence, speech_date: dt.date) -> str:
    """User message for one promise. The speaker is left out on purpose (neutrality)."""
    parts = [
        f"<fecha>{speech_date.isoformat()}</fecha>",
        f"<promesa>{html.escape(claim.text_normalized, quote=False)}</promesa>",
        f"<cita_original>{html.escape(claim.text_original, quote=False)}</cita_original>",
        *(_evidence_block(item) for item in _selected_items(evidence, [])),
    ]
    return "\n".join(parts)


def _empty(
    claim: DetectedClaim, evidence: ClaimEvidence, error: str | None = None
) -> PromiseVerdict:
    if error:
        explanation = "No se pudo completar el análisis de esta promesa."
    else:
        explanation = SEARCH_FAILED if evidence.errors else NO_EVIDENCE
    return PromiseVerdict(
        claim_id=claim.id,
        assessment="no_evaluable",
        confidence=0.0,
        explanation=explanation,
        analysis_error=error,
        evidence_errors=evidence.errors,
    )


def _apply_rules(
    claim: DetectedClaim, evidence: ClaimEvidence, answer: PromiseAnswer
) -> PromiseVerdict:
    by_id = {item.id: item for item in evidence.items}
    cited = [by_id[i] for i in dict.fromkeys(answer.cited_evidence) if i in by_id]
    assessment, confidence, explanation = answer.assessment, answer.confidence, answer.explanation
    if assessment != "no_evaluable" and not cited:
        assessment, explanation = "no_evaluable", explanation + NO_VALID_CITATION
    if assessment == "no_evaluable":
        confidence = 0.0
    elif all(item.source.kind == "web" for item in cited):
        confidence = min(confidence, WEB_ONLY_MAX_CONFIDENCE)
    reasoning = answer.consequences_reasoning.strip()
    return PromiseVerdict(
        claim_id=claim.id,
        assessment=assessment,
        confidence=confidence,
        explanation=explanation,
        obstacles=list(dict.fromkeys(answer.obstacles)),
        consequences_sourced=answer.consequences_sourced.strip() if cited else "",
        consequences_reasoning=f"{REASONING_PREFIX}{reasoning}" if reasoning else "",
        sources=[item.source for item in cited],
        evidence_errors=evidence.errors,
    )


def assess_promises(
    claims: list[DetectedClaim],
    evidences: list[ClaimEvidence],
    *,
    speech_date: dt.date,
    llm: StructuredLLM,
    model: str,
) -> list[PromiseVerdict]:
    """One assessment per promise. Without evidence, `no_evaluable` without calling Claude;
    a failed analysis is recorded, and every analysis failing stops the step."""
    by_claim = {e.claim_id: e for e in evidences}
    results: list[PromiseVerdict] = []
    attempted = failed = 0
    for claim in claims:
        evidence = by_claim.get(claim.id)
        if evidence is None:
            raise PromiseError(f"Falta la evidencia de la promesa {claim.id}")
        if not evidence.items:
            results.append(_empty(claim, evidence))
            continue
        attempted += 1
        logger.info("Promesa %s (%d/%d)", claim.id, len(results) + 1, len(claims))
        try:
            answer = llm.structured(
                step="promises",
                model=model,
                prompt=PROMPT,
                user_content=format_promise(claim, evidence, speech_date),
                output_type=PromiseAnswer,
            )
            results.append(_apply_rules(claim, evidence, answer))
        except LLMConfigError:
            raise
        except (LLMError, ValidationError) as exc:
            logger.warning("Análisis de la promesa %s falló: %s", claim.id, exc)
            failed += 1
            results.append(_empty(claim, evidence, str(exc)))
    if attempted and failed == attempted:
        first = next(r.analysis_error for r in results if r.analysis_error)
        raise PromiseError(f"Todos los análisis de promesas fallaron: {first}")
    return results
