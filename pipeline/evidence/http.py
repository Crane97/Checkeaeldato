"""Peticiones HTTP con reintentos para las fuentes de evidencia."""

from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.evidence.base import EvidenceError

TIMEOUT_SECONDS = 15


class _RetryableStatus(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_attempts: int,
    backoff: float,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying network errors, 429 and 5xx with exponential backoff.

    Returns any other response for the caller to interpret. Raises EvidenceError when
    retries run out. Messages never include the URL, which may carry an API key.
    """
    try:
        for attempt in Retrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=backoff, max=8),
            retry=retry_if_exception_type((httpx.TransportError, _RetryableStatus)),
            reraise=True,
        ):
            with attempt:
                response = client.request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise _RetryableStatus(response.status_code)
    except _RetryableStatus as exc:
        raise EvidenceError(f"{exc} tras {max_attempts} intentos") from exc
    except httpx.TransportError as exc:
        raise EvidenceError(f"error de red: {type(exc).__name__}") from exc
    return response
