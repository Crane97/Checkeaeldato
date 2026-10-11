"""Búsqueda sobre un orador (partido y cargo): dominios oficiales o web general."""

from pipeline.evidence.base import EvidenceConfigError, EvidenceError, SourceResult
from pipeline.evidence.oficiales import MAX_RESULTS, MIN_SCORE, label_results, load_official_domains
from pipeline.evidence.passages import query_terms
from pipeline.evidence.tavily import TavilyClient
from pipeline.models import EvidenceItem


class TavilySpeakerSearch:
    """One Tavily search per call (1 credit): official domains only, or the whole web."""

    def __init__(
        self,
        tavily: TavilyClient,
        domains: dict[str, str] | None = None,
        min_score: float = MIN_SCORE,
    ) -> None:
        self._tavily = tavily
        self._domains = domains if domains is not None else load_official_domains()
        self._min_score = min_score

    def __call__(self, query: str, *, official: bool) -> tuple[list[EvidenceItem], list[str]]:
        found = SourceResult()
        try:
            if official:
                results = self._tavily.search(
                    query, include_domains=list(self._domains), max_results=MAX_RESULTS
                )
            else:
                results = self._tavily.search(query, country="spain", max_results=MAX_RESULTS)
        except EvidenceConfigError:
            raise
        except EvidenceError as exc:
            return [], [f"consulta «{query}»: {exc}"]
        label_results(results, self._domains, self._min_score, found, query_terms(query))
        return found.items, found.errors
