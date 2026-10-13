import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
import respx

from pipeline.costs import search_cost
from pipeline.evidence.base import EvidenceError
from pipeline.evidence.search_cache import SearchCache, cache_key
from pipeline.evidence.tavily import API_URL, TavilyClient

NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)
PAGE = {"title": "EPA", "url": "https://www.ine.es/epa", "content": "Paro 10,5 %", "score": 0.9}


def make(tmp_path: Path, *, read: bool = True, max_age_days: int = 30, **kwargs) -> TavilyClient:
    cache = SearchCache(tmp_path / "cache", max_age_days=max_age_days, read=read, now=lambda: NOW)
    return TavilyClient("tvly-KEY", backoff=0, cache=cache, **kwargs)


def ok(route) -> respx.Route:
    return route.mock(return_value=httpx.Response(200, json={"results": [PAGE]}))


def test_key_normalizes_query_and_ignores_domain_order():
    a = cache_key({"query": "Tasa de  PARO ", "include_domains": ["ine.es", "boe.es"]})
    b = cache_key({"query": "tasa de paro", "include_domains": ["boe.es", "ine.es"]})
    assert a == b
    assert a != cache_key({"query": "tasa de paro", "include_domains": ["ine.es"]})
    assert a != cache_key({"query": "tasa de paro", "max_results": 3})


@respx.mock
def test_repeated_search_is_served_from_disk(tmp_path: Path):
    route = ok(respx.post(API_URL))
    client = make(tmp_path)
    first = client.search("tasa de paro", include_domains=["ine.es"])
    second = make(tmp_path).search("Tasa de paro", include_domains=["ine.es"])
    assert route.call_count == 1
    assert first.results == second.results
    assert (client.stats.real, client.stats.cached, client.stats.credits) == (1, 0, 1)


@respx.mock
def test_stats_count_real_and_cached_searches_and_reset_when_taken(tmp_path: Path):
    ok(respx.post(API_URL))
    client = make(tmp_path)
    client.search("paro")
    client.search("paro")
    taken = client.take_stats()
    assert (taken.real, taken.cached, taken.credits) == (1, 1, 1)
    assert (client.stats.real, client.stats.cached) == (0, 0)


@respx.mock
def test_no_cache_skips_reading_but_refreshes_the_entry(tmp_path: Path):
    route = ok(respx.post(API_URL))
    make(tmp_path).search("paro")
    make(tmp_path, read=False).search("paro")
    assert route.call_count == 2
    make(tmp_path).search("paro")
    assert route.call_count == 2


@respx.mock
def test_expired_entries_are_searched_again(tmp_path: Path):
    route = ok(respx.post(API_URL))
    make(tmp_path).search("paro")
    later = SearchCache(
        tmp_path / "cache", max_age_days=30, now=lambda: NOW + dt.timedelta(days=31)
    )
    TavilyClient("tvly-KEY", backoff=0, cache=later).search("paro")
    assert route.call_count == 2


@respx.mock
def test_errors_are_not_cached(tmp_path: Path):
    route = respx.post(API_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"results": [PAGE]})]
    )
    client = make(tmp_path, max_attempts=1)
    with pytest.raises(EvidenceError):
        client.search("paro")
    client.search("paro")
    assert route.call_count == 2


@respx.mock
def test_corrupt_entry_is_ignored(tmp_path: Path):
    route = ok(respx.post(API_URL))
    make(tmp_path).search("paro")
    [entry] = list((tmp_path / "cache").rglob("*.json"))
    entry.write_text("{no es json")
    make(tmp_path).search("paro")
    assert route.call_count == 2


@respx.mock
def test_entry_keeps_query_parameters_and_date_for_auditing(tmp_path: Path):
    ok(respx.post(API_URL))
    make(tmp_path).search("paro", country="spain")
    [entry] = list((tmp_path / "cache").rglob("*.json"))
    data = json.loads(entry.read_text())
    assert data["request"]["query"] == "paro" and data["request"]["country"] == "spain"
    assert data["saved_at"].startswith("2026-10-13")
    assert "tvly-KEY" not in entry.read_text()


@respx.mock
def test_depth_and_max_results_come_from_the_client_and_advanced_costs_two(tmp_path: Path):
    route = ok(respx.post(API_URL))
    client = make(tmp_path, search_depth="advanced", max_results=3)
    client.search("paro")
    body = json.loads(route.calls[0].request.content)
    assert (body["search_depth"], body["max_results"]) == ("advanced", 3)
    assert client.stats.credits == 2


def test_search_cost_entry():
    entry = search_cost("evidence", real=4, cached=6, credits=4, usd_per_credit=0.01)
    assert (entry.provider, entry.searches, entry.cached_searches, entry.credits) == (
        "tavily",
        4,
        6,
        4,
    )
    assert entry.usd == pytest.approx(0.04)
