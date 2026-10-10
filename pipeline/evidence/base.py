"""Interfaz común de fuentes de evidencia y recolección por afirmación."""

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Protocol

from pipeline.models import ClaimEvidence, DetectedClaim, EvidenceItem

logger = logging.getLogger(__name__)


class EvidenceError(RuntimeError):
    """A source could not be searched."""


class EvidenceConfigError(EvidenceError):
    """Bad key or disabled API: every search will fail, so the job must stop."""


@dataclass
class SourceResult:
    # Item ids are assigned by collect_evidence; sources may leave them empty.
    items: list[EvidenceItem] = field(default_factory=list)
    # Partial failures (e.g. one of several queries), kept for traceability.
    errors: list[str] = field(default_factory=list)


class EvidenceSource(Protocol):
    name: str
    # Skip this source for a claim if earlier sources already found items of these kinds.
    skip_if_found: frozenset[str]

    def search(self, claim: DetectedClaim, speech_date: dt.date) -> SourceResult: ...


def collect_evidence(
    claims: list[DetectedClaim], sources: list[EvidenceSource], speech_date: dt.date
) -> list[ClaimEvidence]:
    """Search every source for every claim, in priority order.

    Duplicated URLs keep the first (higher-priority) source; items are numbered
    E1, E2… per claim for the verdict prompt. A failure for one claim is recorded in
    its `errors`; a config error, or a source failing for every claim, stops the step
    so that "no evidence" never hides a broken source.
    """
    results: list[ClaimEvidence] = []
    runs = {source.name: 0 for source in sources}
    failures = {source.name: 0 for source in sources}

    for claim in claims:
        items: list[EvidenceItem] = []
        errors: list[str] = []
        seen: set[str] = set()
        for source in sources:
            # Fallback sources (e.g. general web) only run if earlier ones found nothing.
            skip_if_found = getattr(source, "skip_if_found", frozenset())
            if any(i.source.kind in skip_if_found for i in items):
                continue
            runs[source.name] += 1
            try:
                found = source.search(claim, speech_date)
            except EvidenceConfigError:
                raise
            except EvidenceError as exc:
                found = SourceResult(errors=[str(exc)])
            except Exception as exc:  # a bug in one source must not lose the other claims
                logger.exception("Fallo inesperado en %s para %s", source.name, claim.id)
                found = SourceResult(errors=[f"error inesperado {type(exc).__name__}: {exc}"])

            errors.extend(f"{source.name}: {e}" for e in found.errors)
            if found.errors and not found.items:
                failures[source.name] += 1
            for item in found.items:
                if item.source.url in seen:
                    continue
                seen.add(item.source.url)
                items.append(item.model_copy(update={"id": f"E{len(items) + 1}"}))

        if errors:
            logger.warning("Evidencia incompleta para %s: %s", claim.id, "; ".join(errors))
        results.append(ClaimEvidence(claim_id=claim.id, items=items, errors=errors))

    for name, count in failures.items():
        if runs[name] and count == runs[name]:
            first = next(e for r in results for e in r.errors if e.startswith(f"{name}: "))
            raise EvidenceError(f"La fuente {name} falló en todas las afirmaciones: {first}")
    return results
