"""Interfaz común de fuentes de evidencia y recolección por afirmación."""

import datetime as dt
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

from rapidfuzz import fuzz

from pipeline.concurrency import parallel_map
from pipeline.models import ClaimEvidence, DetectedClaim, EvidenceItem

logger = logging.getLogger(__name__)

# Below this many searches, failing every time is not proof that a source is down.
MIN_RUNS_TO_ABORT = 3
# Sources that spend web-search credits; skipped when a published check settles the claim.
WEB_SEARCH_SOURCES = frozenset({"oficial", "web"})
# Same similarity threshold as the evaluation's quote matching (0-100).
FACTCHECK_MATCH_THRESHOLD = 80
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_YEAR_RE = re.compile(r"^(19|20)\d\d$")


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


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return re.sub(r"\s+", " ", "".join(c for c in decomposed if not unicodedata.combining(c)))


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUMBER_RE.findall(text)}


def settled_by_factcheck(claim: DetectedClaim, items: list[EvidenceItem]) -> bool:
    """True if a published fact-check reviewed this same claim: similar wording and the
    same figures (a check of another figure or year is another claim)."""
    claim_numbers = _numbers(claim.text_original) | _numbers(claim.text_normalized)
    for item in items:
        if item.source.kind != "factcheck" or not item.claim_reviewed:
            continue
        reviewed = item.claim_reviewed
        similarity = max(
            fuzz.partial_ratio(_plain(reviewed), _plain(text))
            for text in (claim.text_original, claim.text_normalized)
        )
        if similarity < FACTCHECK_MATCH_THRESHOLD:
            continue
        reviewed_numbers = _numbers(reviewed)
        figures = {n for n in claim_numbers if not _YEAR_RE.match(n)}
        # Every figure must match; a year in the check must be one of the claim's years.
        if figures <= reviewed_numbers and reviewed_numbers <= claim_numbers:
            return True
    return False


def _claim_evidence(
    claim: DetectedClaim, sources: list[EvidenceSource], speech_date: dt.date
) -> tuple[ClaimEvidence, dict[str, int], dict[str, int]]:
    """One claim's evidence, with how many times each source ran and failed."""
    runs = {source.name: 0 for source in sources}
    failures = {source.name: 0 for source in sources}
    items: list[EvidenceItem] = []
    errors: list[str] = []
    seen: set[str] = set()
    for source in sources:
        # Fallback sources (e.g. general web) only run if earlier ones found nothing.
        skip_if_found = getattr(source, "skip_if_found", frozenset())
        if any(i.source.kind in skip_if_found for i in items):
            continue
        if source.name in WEB_SEARCH_SOURCES and settled_by_factcheck(claim, items):
            logger.info(
                "%s: ya hay una verificación publicada; no se busca en %s",
                claim.id,
                source.name,
            )
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
    return ClaimEvidence(claim_id=claim.id, items=items, errors=errors), runs, failures


def collect_evidence(
    claims: list[DetectedClaim],
    sources: list[EvidenceSource],
    speech_date: dt.date,
    *,
    max_workers: int = 1,
) -> list[ClaimEvidence]:
    """Search every source for every claim, in priority order (claims in parallel with
    `max_workers` > 1; the results keep the claims' order).

    Duplicated URLs keep the first (higher-priority) source; items are numbered
    E1, E2… per claim for the verdict prompt. A failure for one claim is recorded in
    its `errors`; a config error, or a source failing for every claim, stops the step
    so that "no evidence" never hides a broken source.
    """
    logger.info("Buscando evidencia de %d afirmaciones", len(claims))
    done = parallel_map(lambda c: _claim_evidence(c, sources, speech_date), claims, max_workers)
    results = [r for r, _, _ in done]
    runs = {source.name: sum(d[1][source.name] for d in done) for source in sources}
    failures = {source.name: sum(d[2][source.name] for d in done) for source in sources}

    for name, count in failures.items():
        if runs[name] >= MIN_RUNS_TO_ABORT and count == runs[name]:
            first = next(e for r in results for e in r.errors if e.startswith(f"{name}: "))
            raise EvidenceError(f"La fuente {name} falló en todas las afirmaciones: {first}")
    return results
