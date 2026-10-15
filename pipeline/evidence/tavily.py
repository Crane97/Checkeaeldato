"""Cliente de la API de búsqueda de Tavily (compartido por fuentes oficiales y web)."""

import datetime as dt
import email.utils
import threading
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from pipeline.evidence.base import EvidenceConfigError, EvidenceError
from pipeline.evidence.search_cache import SearchCache
from pipeline.http import TIMEOUT_SECONDS, request_with_retries
from pipeline.models import EvidenceItem, Source, SourceKind

API_URL = "https://api.tavily.com/search"
SearchDepth = Literal["basic", "advanced"]
CREDITS = {"basic": 1, "advanced": 2}


@dataclass(frozen=True)
class TavilyResult:
    title: str
    url: str
    content: str
    score: float | None  # None when Tavily sent no usable score
    published_date: str | None = None
    raw_content: str | None = None  # full page text, when requested


@dataclass(frozen=True)
class TavilySearch:
    results: list[TavilyResult]
    malformed: int = 0  # entries that were not result objects


@dataclass
class SearchStats:
    """Searches made by a client: real (paid) ones and those served from the cache."""

    real: int = 0
    cached: int = 0
    credits: int = 0
    indicator_hits: int = 0  # claims answered by the local base (counted by the pipeline)


def _score(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TavilyClient:
    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        backoff: float = 0.5,
        *,
        search_depth: SearchDepth = "basic",
        max_results: int = 5,
        cache: SearchCache | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._search_depth = search_depth
        self._max_results = max_results
        self._cache = cache
        self.stats = SearchStats()
        self._stats_lock = threading.Lock()  # searches run in parallel (fase I)

    def take_stats(self) -> SearchStats:
        """Counts since the last call (one pipeline step), then reset."""
        with self._stats_lock:
            stats, self.stats = self.stats, SearchStats()
        return stats

    def search(
        self,
        query: str,
        *,
        include_domains: list[str] | None = None,
        country: str | None = None,
        max_results: int | None = None,
        include_raw_content: bool = True,
    ) -> TavilySearch:
        """One search (1 credit basic, 2 advanced), or none if the cache has it. With
        `include_raw_content` each result carries the full page text, so the relevant
        passages can be selected instead of a short snippet."""
        body: dict[str, Any] = {
            "query": query,
            "search_depth": self._search_depth,
            "max_results": max_results or self._max_results,
            "include_published_date": True,
        }
        if include_raw_content:
            body["include_raw_content"] = "text"
        if include_domains:
            body["include_domains"] = include_domains
        if country:
            body["country"] = country

        cached = self._cache.get(body) if self._cache else None
        if cached is not None:
            with self._stats_lock:
                self.stats.cached += 1
            return _parse(cached)
        data = self._fetch(body)
        with self._stats_lock:
            self.stats.real += 1
            self.stats.credits += CREDITS[self._search_depth]
        if self._cache:
            self._cache.put(body, data)
        return _parse(data)

    def _fetch(self, body: dict[str, Any]) -> dict[str, Any]:
        response = request_with_retries(
            self._client,
            "POST",
            API_URL,
            max_attempts=self._max_attempts,
            backoff=self._backoff,
            error_cls=EvidenceError,
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        if response.status_code == 401:
            raise EvidenceConfigError("Tavily respondió 401: revisa TAVILY_API_KEY")
        if response.status_code in (432, 433):
            raise EvidenceConfigError(
                f"Tavily respondió {response.status_code}: se han agotado los créditos del plan"
            )
        if response.status_code != 200:
            raise EvidenceError(f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise EvidenceError("respuesta no es JSON") from exc
        if not isinstance(data, dict) or "error" in data or "detail" in data:
            raise EvidenceError("respuesta inesperada de Tavily")
        if not isinstance(data.get("results"), list):
            raise EvidenceError("respuesta inesperada de Tavily: sin lista de resultados")
        return data


def _parse(data: dict[str, Any]) -> TavilySearch:
    raw = data["results"]
    results = [
        TavilyResult(
            title=str(r.get("title") or ""),
            url=str(r.get("url") or ""),
            content=str(r.get("content") or ""),
            score=_score(r.get("score")),
            published_date=r.get("published_date"),
            raw_content=r.get("raw_content") if isinstance(r.get("raw_content"), str) else None,
        )
        for r in raw
        if isinstance(r, dict)
    ]
    return TavilySearch(results=results, malformed=len(raw) - len(results))


def parse_published_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def to_item(
    result: TavilyResult, organization: str | None, kind: SourceKind, excerpt: str
) -> EvidenceItem | None:
    """Evidence item for a search result; None if the URL is unusable."""
    host = urlparse(result.url).hostname or ""
    try:
        return EvidenceItem(
            id="",
            source=Source(
                url=result.url,
                title=result.title or host,
                organization=organization or host.removeprefix("www."),
                date=parse_published_date(result.published_date),
                kind=kind,
            ),
            excerpt=excerpt,
        )
    except ValidationError:
        return None
