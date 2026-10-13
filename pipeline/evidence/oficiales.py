"""Fuentes oficiales: búsqueda restringida a la lista de dominios oficiales."""

import datetime as dt
import tomllib
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

from pipeline.evidence.base import EvidenceConfigError, EvidenceError, SourceResult
from pipeline.evidence.passages import claim_terms, relevant_excerpt
from pipeline.evidence.tavily import TavilyClient, TavilySearch, to_item
from pipeline.models import DetectedClaim

# Tavily relevance below this is treated as "not found". To be tuned with the eval.
MIN_SCORE = 0.4
MAX_QUERIES = 3


def _load(path: Path | None) -> dict[str, dict[str, str]]:
    if path:
        text = path.read_text()
    else:
        text = files("pipeline.evidence").joinpath("fuentes_oficiales.toml").read_text()
    return tomllib.loads(text)


def load_official_domains(path: Path | None = None) -> dict[str, str]:
    """Domain or suffix -> organization name: every page that counts as official."""
    data = _load(path)
    return {**data.get("sufijos", {}), **data["dominios"]}


def load_search_domains(path: Path | None = None) -> dict[str, str]:
    """The domains official searches are restricted to (no generic suffixes like "gov")."""
    return dict(_load(path)["dominios"])


def official_organization(url: str, domains: dict[str, str]) -> str | None:
    """Organization for an official URL (most specific domain wins), else None."""
    host = (urlparse(url).hostname or "").lower()
    matches = [d for d in domains if host == d or host.endswith("." + d)]
    return domains[max(matches, key=len)] if matches else None


def label_results(
    search: TavilySearch,
    domains: dict[str, str],
    min_score: float,
    found: SourceResult,
    terms: set[str],
) -> None:
    """Add relevant results to `found`, as `oficial` if their domain is official.

    A result without a usable score is kept: dropping it would lose evidence silently.
    """
    if search.malformed:
        found.errors.append(f"{search.malformed} resultado(s) malformado(s) descartado(s)")
    seen = {i.source.url for i in found.items}
    for r in search.results:
        if (r.score is not None and r.score < min_score) or r.url in seen:
            continue
        organization = official_organization(r.url, domains)
        excerpt = relevant_excerpt(r.content, r.raw_content, terms)
        item = to_item(r, organization, "oficial" if organization else "web", excerpt)
        if item is not None:
            seen.add(item.source.url)
            found.items.append(item)


def queries_for(claim: DetectedClaim, limit: int) -> list[str]:
    return claim.search_queries[:limit] or [claim.text_normalized]


class OfficialSearchSource:
    """Web search restricted to official domains (INE, BOE, Banco de España…)."""

    name = "oficial"
    skip_if_found: frozenset[str] = frozenset()

    def __init__(
        self,
        tavily: TavilyClient,
        domains: dict[str, str] | None = None,
        min_score: float = MIN_SCORE,
        max_queries: int = MAX_QUERIES,
    ) -> None:
        self._tavily = tavily
        self._domains = domains if domains is not None else load_official_domains()
        self._search_domains = list(domains if domains is not None else load_search_domains())
        self._min_score = min_score
        self._max_queries = max_queries

    def search(self, claim: DetectedClaim, speech_date: dt.date) -> SourceResult:
        found = SourceResult()
        for query in queries_for(claim, self._max_queries):
            try:
                results = self._tavily.search(query, include_domains=self._search_domains)
            except EvidenceConfigError:
                raise
            except EvidenceError as exc:
                found.errors.append(f"consulta «{query}»: {exc}")
                continue
            label_results(results, self._domains, self._min_score, found, claim_terms(claim))
        return found
