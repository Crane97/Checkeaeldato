"""Google Fact Check Tools API (ClaimReview de Maldita, Newtral, EFE Verifica…)."""

import datetime as dt
import logging
from typing import Any

import httpx
from pydantic import ValidationError
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.evidence.base import EvidenceConfigError, EvidenceError, SourceResult
from pipeline.models import DetectedClaim, EvidenceItem, Source

logger = logging.getLogger(__name__)

API_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
LANGUAGE = "es"
PAGE_SIZE = 5
TIMEOUT_SECONDS = 15


class _RetryableStatus(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def _parse_date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


class FactCheckSource:
    """Published fact-checks matching the claim's literal quote or its search queries."""

    name = "factcheck"

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

    def search(self, claim: DetectedClaim, speech_date: dt.date) -> SourceResult:
        result = SourceResult()
        seen: set[str] = set()
        for query in self._queries(claim):
            try:
                data = self._get(query)
            except EvidenceConfigError:
                raise
            except EvidenceError as exc:
                result.errors.append(f"consulta «{query}»: {exc}")
                continue
            for entry in data.get("claims", []):
                for review in entry.get("claimReview", []):
                    item = self._to_item(entry, review)
                    if item is not None and item.source.url not in seen:
                        seen.add(item.source.url)
                        result.items.append(item)
        return result

    @staticmethod
    def _queries(claim: DetectedClaim) -> list[str]:
        queries: list[str] = []
        for q in [claim.text_original, *claim.search_queries]:
            q = q.strip()
            if q and q.casefold() not in {x.casefold() for x in queries}:
                queries.append(q)
        return queries

    def _get(self, query: str) -> dict[str, Any]:
        params = {"query": query, "languageCode": LANGUAGE, "pageSize": PAGE_SIZE}
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=self._backoff, max=8),
                retry=retry_if_exception_type((httpx.TransportError, _RetryableStatus)),
                reraise=True,
            ):
                with attempt:
                    response = self._client.get(API_URL, params={**params, "key": self._api_key})
                    if response.status_code == 429 or response.status_code >= 500:
                        raise _RetryableStatus(response.status_code)
        except _RetryableStatus as exc:
            raise EvidenceError(f"{exc} tras {self._max_attempts} intentos") from exc
        except httpx.TransportError as exc:
            raise EvidenceError(f"error de red: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            raise EvidenceConfigError(
                f"Google Fact Check respondió {response.status_code}: revisa "
                "GOOGLE_FACTCHECK_API_KEY y que la Fact Check Tools API esté habilitada"
            )
        if response.status_code != 200:
            raise EvidenceError(f"HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise EvidenceError("respuesta no es JSON") from exc

    @staticmethod
    def _to_item(entry: dict[str, Any], review: dict[str, Any]) -> EvidenceItem | None:
        publisher = review.get("publisher") or {}
        organization = publisher.get("name") or publisher.get("site") or "Verificador"
        claim_text = entry.get("text")
        rating = review.get("textualRating")
        excerpt = f"{organization} revisó la afirmación «{claim_text or '¿?'}»"
        if entry.get("claimant"):
            excerpt += f", atribuida a {entry['claimant']}"
        excerpt += f", y la valoró como «{rating}»." if rating else "."
        try:
            return EvidenceItem(
                id="",
                source=Source(
                    url=review.get("url") or "",
                    title=review.get("title") or claim_text or organization,
                    organization=organization,
                    date=_parse_date(review.get("reviewDate")),
                    kind="factcheck",
                ),
                excerpt=excerpt,
                claim_reviewed=claim_text,
                rating=rating,
            )
        except ValidationError:
            logger.debug("Revisión sin URL válida descartada: %s", review.get("url"))
            return None
