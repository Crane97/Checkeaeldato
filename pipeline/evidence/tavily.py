"""Cliente de la API de búsqueda de Tavily (compartido por fuentes oficiales y web)."""

import datetime as dt
import email.utils
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from pipeline.evidence.base import EvidenceConfigError, EvidenceError
from pipeline.evidence.http import TIMEOUT_SECONDS, request_with_retries
from pipeline.models import EvidenceItem, Source, SourceKind

API_URL = "https://api.tavily.com/search"
EXCERPT_CHARS = 1200


@dataclass(frozen=True)
class TavilyResult:
    title: str
    url: str
    content: str
    score: float
    published_date: str | None = None


class TavilyClient:
    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        backoff: float = 0.5,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._max_attempts = max_attempts
        self._backoff = backoff

    def search(
        self,
        query: str,
        *,
        include_domains: list[str] | None = None,
        country: str | None = None,
        max_results: int = 5,
    ) -> list[TavilyResult]:
        """One basic search (1 credit)."""
        body: dict[str, Any] = {
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_published_date": True,
        }
        if include_domains:
            body["include_domains"] = include_domains
        if country:
            body["country"] = country

        response = request_with_retries(
            self._client,
            "POST",
            API_URL,
            max_attempts=self._max_attempts,
            backoff=self._backoff,
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
        return [
            TavilyResult(
                title=r.get("title") or "",
                url=r.get("url") or "",
                content=r.get("content") or "",
                score=float(r.get("score") or 0),
                published_date=r.get("published_date"),
            )
            for r in data.get("results", [])
        ]


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
    result: TavilyResult, organization: str | None, kind: SourceKind
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
            excerpt=result.content[:EXCERPT_CHARS],
        )
    except ValidationError:
        return None
