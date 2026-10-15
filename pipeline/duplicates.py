"""Afirmaciones casi idénticas del mismo orador en el mismo análisis: se verifican una vez.

En un debate es habitual repetir el mismo dato varias veces. Buscar evidencia y decidir el
veredicto de cada repetición gasta búsquedas y llamadas sin aportar nada, y puede dar
veredictos distintos a la misma frase. La primera aparición es la que se verifica; las
demás comparten su veredicto y conservan su cita y su momento.
"""

import re
from typing import TypeVar

from rapidfuzz import fuzz, utils

from pipeline.models import DetectedClaim, PromiseVerdict, Verdict

V = TypeVar("V", Verdict, PromiseVerdict)

# Similarity (0-100) of the normalized texts, ignoring word order, case and punctuation.
DUPLICATE_THRESHOLD = 90
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _signature(claim: DetectedClaim) -> tuple:
    """What must be identical for two claims to be the same: who, what kind, which figures,
    and the actor and periods the context step resolved."""
    numbers = frozenset(n.replace(",", ".") for n in _NUMBER_RE.findall(claim.text_normalized))
    scope = tuple((r.start, r.end) for r in claim.time_scope)
    return (claim.speaker, claim.kind, numbers, claim.resolved_actor, scope)


def mark_duplicates(claims: list[DetectedClaim]) -> list[DetectedClaim]:
    """Same claims, with `duplicate_of` set on repetitions (pointing to the first one)."""
    firsts: list[DetectedClaim] = []
    result: list[DetectedClaim] = []
    for claim in claims:
        original = next(
            (
                f
                for f in firsts
                if _signature(f) == _signature(claim)
                and fuzz.token_sort_ratio(
                    f.text_normalized, claim.text_normalized, processor=utils.default_process
                )
                >= DUPLICATE_THRESHOLD
            ),
            None,
        )
        if original is None:
            firsts.append(claim)
            result.append(claim.model_copy(update={"duplicate_of": None}))
        else:
            result.append(claim.model_copy(update={"duplicate_of": original.id}))
    return result


def with_repetitions(claims: list[DetectedClaim], results: list[V]) -> list[V]:
    """Results for every claim, in order: a repetition gets a copy of its original's."""
    by_id = {r.claim_id: r for r in results}
    return [
        by_id[c.id]
        if c.duplicate_of is None
        else by_id[c.duplicate_of].model_copy(update={"claim_id": c.id})
        for c in claims
        if c.id in by_id or c.duplicate_of in by_id
    ]
