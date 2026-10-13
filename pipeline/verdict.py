"""Veredicto basado solo en la evidencia recuperada."""

import datetime as dt
import html
import logging
import re
from collections.abc import Callable
from functools import lru_cache
from typing import Protocol, TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator

from pipeline.evidence.base import settled_by_factcheck
from pipeline.evidence.oficiales import is_primary, load_primary_sources
from pipeline.governments import Governments
from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import (
    ClaimEvidence,
    DetectedClaim,
    EvidenceItem,
    Source,
    SpeakerMap,
    Verdict,
    VerdictAnalysis,
    VerdictLabel,
)
from pipeline.neutrality import FORBIDDEN_WORDS_RE

logger = logging.getLogger(__name__)

PROMPT = "veredicto.md"
# CLAUDE.md: a verdict resting only on general web sources carries less confidence.
WEB_ONLY_MAX_CONFIDENCE = 0.6
# A second, targeted search round when the first evidence cannot settle the claim.
MAX_FOLLOW_UP_QUERIES = 3
# Token budget of the verdict call (the most expensive step): the evidence sent is capped,
# reliable sources first; the full evidence stays in evidence.json.
MAX_EVIDENCE_ITEMS = 8
VERDICT_EXCERPT_CHARS = 2500
_KIND_PRIORITY = {"factcheck": 0, "oficial": 1, "web": 2}

# (claim, queries, first evidence number) -> (new items numbered from it, errors)
FollowUpSearch = Callable[[DetectedClaim, list[str], int], tuple[list[EvidenceItem], list[str]]]
# (claim, source) -> True if the source speaks for the claim's speaker or their Government
# (e.g. La Moncloa when the president speaks): neutral, it never counts for the verdict.
OwnSource = Callable[[DetectedClaim, Source], bool]

_EVIDENCE_ID_RE = re.compile(r"\bE\d+\b")
# Wording that says the evidence matches the claim, or that it does not. A negation right
# before a positive phrase ("no coincide") turns it into a negative one.
_POSITIVE_RE = re.compile(
    r"\b(coincide[n]?|respalda[n]?|es correct[ao]|son correct[ao]s|se ajusta[n]?)\b",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"\b(contradice[n]?|es fals[ao]|es incorrect[ao]|no coincide[n]?|no respalda[n]?|"
    r"no se ajusta[n]?|no es correct[ao])\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE_RE = re.compile(r"\b(no|ni|tampoco|nunca)\s+(\w+\s+)?$", re.IGNORECASE)

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


# Schema of Claude's answer (see prompts/veredicto.md). The order matters: the model
# writes the evidence it relies on and its analysis before the label.
class VerdictAnswer(BaseModel):
    evidence_used: list[str]
    analysis: VerdictAnalysis
    numeric_check: str
    verdict: VerdictLabel
    confidence: float = Field(ge=0, le=1)
    explanation: str
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


# The Government speaking for itself: La Moncloa and ministries' press rooms. Statistics
# offices and official gazettes are not included (they publish data, not positions).
_GOVERNMENT_HOSTS = ("lamoncloa.gob.es",)
_PRESS_PATH_RE = re.compile(
    r"/(prensa|notas?[ _%20-]*de[ _%20-]*prensa|comunicacion|gabinete[ _-]*de[ _-]*prensa)(/|$)",
    re.IGNORECASE,
)


def is_government_voice(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _GOVERNMENT_HOSTS):
        return True
    return host.endswith(".gob.es") and bool(_PRESS_PATH_RE.search(parsed.path))


def own_source_checker(
    speakers: SpeakerMap, governments: Governments, speech_date: dt.date
) -> OwnSource:
    """Sources that speak for the claim's speaker: the Government's own communication when
    the speaker belongs to a party in that Government on the speech date."""
    term = governments.at(speech_date)
    governing = set(term.parties if term else []) | set(governments.partners_at(speech_date))

    def check(claim: DetectedClaim, source: Source) -> bool:
        speaker = speakers.get(claim.speaker)
        return bool(speaker and speaker.party in governing and is_government_voice(source.url))

    return check


def _evidence_block(item: EvidenceItem, own: bool = False) -> str:
    source = item.source
    attrs = {
        "id": item.id,
        "tipo": source.kind,
        "organismo": source.organization,
        "fecha": source.date.isoformat() if source.date else "desconocida",
        "url": source.url,
    }
    if own:
        attrs["del_gobierno_del_orador"] = "sí"
    attr_text = " ".join(f'{k}="{html.escape(v, quote=True)}"' for k, v in attrs.items())
    lines = [f"título: {source.title}"]
    if item.claim_reviewed:
        lines.append(f"afirmación revisada por el verificador: {item.claim_reviewed}")
    if item.rating:
        lines.append(f"valoración del verificador: {item.rating}")
    lines.append(f"extracto: {item.excerpt[:VERDICT_EXCERPT_CHARS]}")
    body = html.escape("\n".join(lines), quote=False)
    return f"<evidencia {attr_text}>\n{body}\n</evidencia>"


def _selected_items(evidence: ClaimEvidence, prefer: list[str]) -> list[EvidenceItem]:
    """Up to MAX_EVIDENCE_ITEMS: preferred ids (new follow-up evidence) first, then
    fact-checks, official and web sources, keeping their order within each kind."""
    ranked = sorted(
        evidence.items,
        key=lambda i: (i.id not in prefer, _KIND_PRIORITY.get(i.source.kind, 3)),
    )
    chosen = {i.id for i in ranked[:MAX_EVIDENCE_ITEMS]}
    return [i for i in evidence.items if i.id in chosen]


def format_claim(
    claim: DetectedClaim,
    evidence: ClaimEvidence,
    speech_date: dt.date,
    final: bool = False,
    prefer: list[str] | None = None,
    own_ids: frozenset[str] = frozenset(),
    warning: str | None = None,
) -> str:
    """User message for one claim. The speaker is left out on purpose (neutrality); only
    sources from the speaker's own Government are flagged, so they are not used as proof."""
    parts = [
        f"<fecha>{speech_date.isoformat()}</fecha>",
        f"<afirmacion>{html.escape(claim.text_normalized, quote=False)}</afirmacion>",
        f"<cita_original>{html.escape(claim.text_original, quote=False)}</cita_original>",
        f"<tipo>{claim.claim_type}</tipo>",
        *(
            [
                f"<actor_resuelto>{html.escape(claim.resolved_actor or '', quote=False)}"
                "</actor_resuelto>",
                f"<periodos>{'; '.join(r.label() for r in claim.time_scope)}</periodos>",
            ]
            if claim.context_status == "resuelto"
            else []
        ),
        *(
            _evidence_block(item, item.id in own_ids)
            for item in _selected_items(evidence, prefer or [])
        ),
    ]
    if warning:
        parts.append(f"<aviso>{html.escape(warning, quote=False)}</aviso>")
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


def _unresolved_context(claim: DetectedClaim, evidence: ClaimEvidence) -> Verdict:
    reason = claim.resolution_notes or "No se ha podido resolver el contexto."
    return Verdict(
        claim_id=claim.id,
        verdict="no_verificable",
        confidence=0.0,
        explanation=f"No se puede verificar sin saber a quién o a qué periodo se refiere. {reason}",
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


@lru_cache
def _primary_sources() -> tuple[str, ...]:
    return tuple(load_primary_sources())


def _source_rank(item: EvidenceItem) -> int:
    """Primary sources (legal texts, statistics) first, then other official, fact-checks, web."""
    if item.source.kind == "oficial":
        return 0 if is_primary(item.source.url, list(_primary_sources())) else 1
    return {"factcheck": 2, "web": 3}.get(item.source.kind, 4)


def _mentioned_ids(answer: VerdictAnswer) -> list[str]:
    analysis = answer.analysis
    texts = [
        answer.explanation,
        answer.numeric_check,
        analysis.claim_parsed,
        analysis.numeric_comparison,
        analysis.nuances,
        analysis.conclusion,
        *(a.says for a in analysis.evidence_assessment),
    ]
    ids = [a.id for a in analysis.evidence_assessment]
    ids += [m for t in texts for m in _EVIDENCE_ID_RE.findall(t)]
    return list(dict.fromkeys(ids))


def _polarity(text: str) -> tuple[bool, bool]:
    """(says it matches, says it does not), with negated positives counted as negative."""
    negative = bool(_NEGATIVE_RE.search(text))
    positive = False
    for match in _POSITIVE_RE.finditer(text):
        if _NEGATION_BEFORE_RE.search(text[: match.start()]):
            negative = True
        else:
            positive = True
    return positive, negative


def _incoherences(answer: VerdictAnswer, used: list[str], own_ids: frozenset[str]) -> list[str]:
    """Reasons why the label does not follow from the analysis and the explanation."""
    reasons: list[str] = []
    stances = {
        a.id: ("neutral" if a.id in own_ids else a.stance)
        for a in answer.analysis.evidence_assessment
    }
    counted = [stances.get(i, "neutral") for i in used]  # own sources count as neutral
    if answer.verdict == "falso" and "contradice" not in counted:
        reasons.append(
            "el veredicto es falso, pero ninguna fuente usada contradice la afirmación "
            "(stance «contradice»)"
        )
    if answer.verdict == "verdadero" and "apoya" not in counted:
        reasons.append(
            "el veredicto es verdadero, pero ninguna fuente usada apoya la afirmación "
            "(stance «apoya»)"
        )
    positive, negative = _polarity(f"{answer.explanation} {answer.analysis.conclusion}")
    if answer.verdict == "falso" and positive and not negative:
        reasons.append(
            "la explicación dice que la evidencia coincide con la afirmación, pero el "
            "veredicto es falso"
        )
    if answer.verdict == "verdadero" and negative and not positive:
        reasons.append(
            "la explicación dice que la evidencia contradice la afirmación, pero el "
            "veredicto es verdadero"
        )
    return reasons


def _review(claim: DetectedClaim, evidence: ClaimEvidence, reasons: list[str], **extra) -> Verdict:
    logger.warning("%s necesita revisión: %s", claim.id, "; ".join(reasons))
    return Verdict(
        claim_id=claim.id,
        verdict="no_verificable",
        confidence=0.0,
        explanation=(
            "El análisis automático no fue coherente y queda pendiente de revisión; "
            "por ahora no se puede dar un veredicto."
        ),
        evidence_errors=evidence.errors,
        needs_review=True,
        review_reasons=reasons,
        **extra,
    )


def _apply_rules(
    claim: DetectedClaim,
    evidence: ClaimEvidence,
    answer: VerdictAnswer,
    follow_up_queries: list[str] | None = None,
    *,
    own_ids: frozenset[str] = frozenset(),
    attempts: int = 1,
    thinking: str | None = None,
) -> Verdict:
    """Turn Claude's answer into a Verdict that respects CLAUDE.md's principles.
    Incoherent answers are not handled here: see `_incoherences`."""
    by_id = {item.id: item for item in evidence.items}
    audit = {
        "evidence_used": answer.evidence_used,
        "analysis": answer.analysis,
        "numeric_check": answer.numeric_check,
        "thinking_summary": thinking,
        "attempts": attempts,
        "follow_up_queries": follow_up_queries or [],
    }
    unknown = [i for i in [*answer.evidence_used, *_mentioned_ids(answer)] if i not in by_id]
    if unknown:
        ids = ", ".join(dict.fromkeys(unknown))
        return _review(claim, evidence, [f"cita evidencia que no existe: {ids}"], **audit)

    # Sources: what the answer says it used, plus any other evidence it mentions, by rank.
    used = list(dict.fromkeys([*answer.evidence_used, *_mentioned_ids(answer)]))
    cited = sorted((by_id[i] for i in used), key=_source_rank)

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
        **audit,
    )


def _decide(
    claim: DetectedClaim,
    evidence: ClaimEvidence,
    speech_date: dt.date,
    llm: StructuredLLM,
    model: str,
    follow_up: FollowUpSearch | None,
    own_source: OwnSource | None = None,
    thinking: bool | None = None,
) -> Verdict:
    def own(ev: ClaimEvidence) -> frozenset[str]:
        if own_source is None:
            return frozenset()
        return frozenset(i.id for i in ev.items if own_source(claim, i.source))

    def ask(
        ev: ClaimEvidence, final: bool, prefer: list[str] | None = None, warning: str | None = None
    ) -> VerdictAnswer:
        content = format_claim(
            claim, ev, speech_date, final=final, prefer=prefer, own_ids=own(ev), warning=warning
        )
        kwargs = {"thinking": thinking} if thinking is not None else {}
        return llm.structured(
            step="verdicts",
            model=model,
            prompt=PROMPT,
            user_content=content,
            output_type=VerdictAnswer,
            **kwargs,
        )

    def checked(
        ev: ClaimEvidence,
        first: VerdictAnswer,
        final: bool,
        prefer: list[str] | None = None,
        queries: list[str] | None = None,
    ) -> Verdict:
        """Apply the rules; an incoherent answer is asked once more, then sent to review."""
        own_ids = own(ev)
        answer, attempts = first, 1
        reasons = _incoherences(answer, answer.evidence_used, own_ids)
        if reasons and not _unknown_ids(answer, ev):
            logger.info("%s: respuesta incoherente, se repite: %s", claim.id, "; ".join(reasons))
            warning = (
                "Tu respuesta anterior era incoherente: " + "; ".join(reasons) + ". Revisa la "
                "evidencia y decide de nuevo; el veredicto debe derivarse de tu conclusión."
            )
            answer, attempts = ask(ev, final=final, prefer=prefer, warning=warning), 2
            reasons = _incoherences(answer, answer.evidence_used, own_ids)
            if reasons and not _unknown_ids(answer, ev):
                return _review(
                    claim,
                    ev,
                    reasons,
                    evidence_used=answer.evidence_used,
                    analysis=answer.analysis,
                    numeric_check=answer.numeric_check,
                    attempts=attempts,
                    follow_up_queries=queries or [],
                )
        return _apply_rules(
            claim,
            ev,
            answer,
            queries,
            own_ids=own_ids,
            attempts=attempts,
            thinking=getattr(llm, "last_thinking", None),
        )

    answer = ask(evidence, final=follow_up is None)
    queries = [q.strip() for q in answer.follow_up_queries if q.strip()][:MAX_FOLLOW_UP_QUERIES]
    settled = settled_by_factcheck(claim, evidence.items)
    if answer.verdict != "no_verificable" or not queries or follow_up is None or settled:
        return checked(evidence, answer, final=follow_up is None)

    logger.info("%s: búsqueda adicional (%s)", claim.id, "; ".join(queries))
    items, errors = follow_up(claim, queries, len(evidence.items) + 1)
    known = {i.source.url for i in evidence.items}
    items = [i for i in items if i.source.url not in known]
    extended = evidence.model_copy(
        update={"items": [*evidence.items, *items], "errors": [*evidence.errors, *errors]}
    )
    if not items:
        return checked(extended, answer, final=True, queries=queries)
    prefer = [i.id for i in items]
    final = ask(extended, final=True, prefer=prefer)
    return checked(extended, final, final=True, prefer=prefer, queries=queries)


def _unknown_ids(answer: VerdictAnswer, evidence: ClaimEvidence) -> bool:
    known = {i.id for i in evidence.items}
    return any(i not in known for i in [*answer.evidence_used, *_mentioned_ids(answer)])


def decide_verdicts(
    claims: list[DetectedClaim],
    evidences: list[ClaimEvidence],
    *,
    speech_date: dt.date,
    llm: StructuredLLM,
    model: str,
    follow_up: FollowUpSearch | None = None,
    own_source: OwnSource | None = None,
    thinking: bool | None = None,
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
        if claim.context_status == "no_resuelto":
            verdicts.append(_unresolved_context(claim, evidence))
            continue
        if not evidence.items:
            verdicts.append(_no_evidence(claim, evidence))
            continue

        attempted += 1
        logger.info("Veredicto de %s (%d/%d)", claim.id, len(verdicts) + 1, len(claims))
        try:
            verdicts.append(
                _decide(claim, evidence, speech_date, llm, model, follow_up, own_source, thinking)
            )
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
