import datetime as dt

import httpx
import pytest
import respx

from pipeline.evidence.base import EvidenceConfigError
from pipeline.evidence.factcheck_api import API_URL, FactCheckSource
from pipeline.models import DetectedClaim

DATE = dt.date(2026, 11, 20)


def claim(queries: list[str] | None = None) -> DetectedClaim:
    return DetectedClaim(
        id="c001",
        speaker="speaker_0",
        text_original="el paro ha bajado un 20 %",
        text_normalized="El paro en España ha bajado un 20 %.",
        claim_type="estadistica",
        search_queries=queries if queries is not None else ["tasa de paro España 2026"],
    )


def review(url: str, rating: str = "Falso", **overrides) -> dict:
    data = {
        "publisher": {"name": "Maldita.es", "site": "maldita.es"},
        "url": url,
        "title": "No, el paro no ha bajado un 20 %",
        "reviewDate": "2026-09-01T00:00:00Z",
        "textualRating": rating,
        "languageCode": "es",
    }
    return data | overrides


def response(*reviews: dict, text: str = "El paro ha bajado un 20 %") -> dict:
    return {
        "claims": [
            {
                "text": text,
                "claimant": "Un orador",
                "claimDate": "2026-08-30T00:00:00Z",
                "claimReview": list(reviews),
            }
        ]
    }


def source(**kwargs) -> FactCheckSource:
    return FactCheckSource(api_key="KEY", backoff=0, **kwargs)


@respx.mock
def test_sends_spanish_queries_with_key_and_parses_reviews():
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(200, json=response(review("https://maldita.es/a")))
    )

    result = source().search(claim(), DATE)

    params = [dict(call.request.url.params) for call in route.calls]
    assert [p["query"] for p in params] == ["el paro ha bajado un 20 %", "tasa de paro España 2026"]
    assert all(p["languageCode"] == "es" and p["key"] == "KEY" for p in params)
    assert result.errors == []
    [item] = result.items  # same review from both queries: deduplicated
    assert item.source.url == "https://maldita.es/a"
    assert item.source.kind == "factcheck"
    assert item.source.organization == "Maldita.es"
    assert item.source.title == "No, el paro no ha bajado un 20 %"
    assert item.source.date == dt.date(2026, 9, 1)
    assert item.rating == "Falso"
    assert item.claim_reviewed == "El paro ha bajado un 20 %"
    assert "Maldita.es" in item.excerpt and "Falso" in item.excerpt


@respx.mock
def test_duplicate_queries_are_sent_once():
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, json={}))
    source().search(claim(["El paro ha bajado un 20 %", " "]), DATE)
    assert len(route.calls) == 1


@respx.mock
def test_empty_response_means_no_items():
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={}))
    assert source().search(claim(), DATE).items == []


@respx.mock
def test_entries_without_valid_url_are_skipped():
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            200,
            json=response(review(""), review("ftp://x"), review("https://maldita.es/ok")),
        )
    )
    result = source().search(claim([]), DATE)
    assert [i.source.url for i in result.items] == ["https://maldita.es/ok"]


@respx.mock
def test_missing_optional_fields_are_tolerated():
    bare = {"url": "https://newtral.es/x", "publisher": {"site": "newtral.es"}}
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=response(bare)))
    [item] = source().search(claim([]), DATE).items
    assert item.source.organization == "newtral.es"
    assert item.source.date is None
    assert item.rating is None


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
def test_auth_errors_stop_with_config_error(status):
    respx.get(API_URL).mock(return_value=httpx.Response(status, json={"error": {}}))
    with pytest.raises(EvidenceConfigError, match="GOOGLE_FACTCHECK_API_KEY"):
        source().search(claim(), DATE)


@respx.mock
def test_transient_errors_are_retried():
    route = respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectError("reset"),
            httpx.Response(200, json=response(review("https://maldita.es/a"))),
            httpx.Response(200, json={}),
        ]
    )
    result = source().search(claim(), DATE)
    assert len(result.items) == 1
    assert len(route.calls) == 4


@respx.mock
def test_persistent_failure_of_one_query_is_recorded_and_others_kept():
    respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json=response(review("https://maldita.es/a"))),
        ]
    )
    result = source(max_attempts=3).search(claim(), DATE)
    assert len(result.items) == 1
    [error] = result.errors
    assert "el paro ha bajado un 20 %" in error and "500" in error


@respx.mock
def test_bad_request_is_not_retried():
    route = respx.get(API_URL).mock(return_value=httpx.Response(400))
    result = source().search(claim([]), DATE)
    assert len(route.calls) == 1
    assert "400" in result.errors[0]


@respx.mock
def test_errors_never_include_the_api_key():
    respx.get(API_URL).mock(side_effect=httpx.ConnectError("https://x?key=KEY failed"))
    result = source(max_attempts=1).search(claim([]), DATE)
    assert result.errors and all("KEY" not in e for e in result.errors)


@pytest.mark.parametrize("payload", [{"error": {"code": 500}}, {"claims": None}, []])
@respx.mock
def test_malformed_200_is_recorded_as_error(payload):
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=payload))
    result = source().search(claim([]), DATE)
    assert result.items == []
    assert "inesperada" in result.errors[0]


@respx.mock
def test_invalid_key_reported_as_400_stops_with_config_error():
    body = {
        "error": {
            "code": 400,
            "message": "API key not valid.",
            "status": "INVALID_ARGUMENT",
            "details": [{"reason": "API_KEY_INVALID"}],
        }
    }
    respx.get(API_URL).mock(return_value=httpx.Response(400, json=body))
    with pytest.raises(EvidenceConfigError, match="GOOGLE_FACTCHECK_API_KEY"):
        source().search(claim(), DATE)


@respx.mock
def test_odd_entries_are_skipped():
    payload = {
        "claims": [
            "x",
            {"text": "t", "claimReview": None},
            response(review("https://maldita.es/a"))["claims"][0],
        ]
    }
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=payload))
    assert [i.source.url for i in source().search(claim([]), DATE).items] == [
        "https://maldita.es/a"
    ]
