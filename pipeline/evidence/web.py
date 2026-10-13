"""Búsqueda web general: último recurso si no hay evidencia oficial."""

import datetime as dt
import logging

from pipeline.evidence.base import EvidenceConfigError, EvidenceError, SourceResult
from pipeline.evidence.oficiales import (
    MIN_SCORE,
    label_results,
    load_official_domains,
    queries_for,
)
from pipeline.evidence.passages import claim_terms
from pipeline.evidence.tavily import TavilyClient
from pipeline.models import DetectedClaim

logger = logging.getLogger(__name__)

# Per claim, counting the follow-up round (decision of the project owner, 2026-10-13).
MAX_WEB_SEARCHES_PER_CLAIM = 2


class WebSearchSource:
    """General web search, run only when official sources found nothing relevant.

    Results from official domains are still labelled `oficial`; the rest are `web`.
    There is no list of preferred media yet (a pending neutrality decision).
    """

    name = "web"
    skip_if_found: frozenset[str] = frozenset({"oficial"})

    def __init__(
        self,
        tavily: TavilyClient,
        domains: dict[str, str] | None = None,
        min_score: float = MIN_SCORE,
    ) -> None:
        self._tavily = tavily
        self._domains = domains if domains is not None else load_official_domains()
        self._min_score = min_score
        self._searches: dict[str, int] = {}

    def search(self, claim: DetectedClaim, speech_date: dt.date) -> SourceResult:
        found = SourceResult()
        if self._searches.get(claim.id, 0) >= MAX_WEB_SEARCHES_PER_CLAIM:
            logger.info("%s: límite de búsquedas web alcanzado", claim.id)
            return found
        self._searches[claim.id] = self._searches.get(claim.id, 0) + 1
        [query] = queries_for(claim, 1)
        try:
            results = self._tavily.search(query, country="spain")
        except EvidenceConfigError:
            raise
        except EvidenceError as exc:
            found.errors.append(f"consulta «{query}»: {exc}")
            return found
        label_results(results, self._domains, self._min_score, found, claim_terms(claim))
        return found
