import datetime as dt
import json

import httpx
import pytest
import respx

from pipeline.evidence.base import (
    EvidenceConfigError,
    EvidenceError,
    SourceResult,
    collect_evidence,
)
from pipeline.evidence.oficiales import (
    OfficialSearchSource,
    load_official_domains,
    official_organization,
)
from pipeline.evidence.tavily import API_URL, TavilyClient
from pipeline.evidence.web import WebSearchSource
from pipeline.models import DetectedClaim, EvidenceItem, Source

DATE = dt.date(2026, 11, 20)
DOMAINS = {"ine.es": "INE", "gob.es": "Gobierno", "hacienda.gob.es": "Hacienda"}


def claim(queries: list[str] | None = None) -> DetectedClaim:
    return DetectedClaim(
        id="c001",
        speaker="autor",
        text_original="el paro ha bajado un 20 %",
        text_normalized="El paro en España ha bajado un 20 % desde 2024.",
        claim_type="estadistica",
        search_queries=["tasa de paro EPA 2026", "parados España 2026", "tercera"]
        if queries is None
        else queries,
    )


def result(url: str, score: float = 0.8, **extra) -> dict:
    return {
        "title": "Encuesta de Población Activa",
        "url": url,
        "content": "La tasa de paro se situó en el 10,5 %.",
        "score": score,
    } | extra


def tavily(**kwargs) -> TavilyClient:
    return TavilyClient(api_key="tvly-KEY", backoff=0, **kwargs)


# --- official domains -------------------------------------------------------


def test_bundled_official_domains_load():
    domains = load_official_domains()
    assert domains["ine.es"].startswith("Instituto Nacional de Estadística")
    assert "boe.es" in domains


@pytest.mark.parametrize(
    ("url", "org"),
    [
        ("https://www.ine.es/prensa/epa.pdf", "INE"),
        ("https://ine.es/x", "INE"),
        ("https://www.hacienda.gob.es/x", "Hacienda"),  # most specific domain wins
        ("https://www.sanidad.gob.es/x", "Gobierno"),
        ("https://falsoine.es/x", None),
        ("https://ine.es.ejemplo.com/x", None),
    ],
)
def test_official_organization_matches_whole_domains(url, org):
    assert official_organization(url, DOMAINS) == org


# --- Tavily client ----------------------------------------------------------


@respx.mock
def test_tavily_sends_bearer_key_and_parses_results():
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(200, json={"results": [result("https://www.ine.es/a")]})
    )
    [r] = tavily().search("tasa de paro", include_domains=["ine.es"], max_results=4).results

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer tvly-KEY"
    body = json.loads(request.content)
    assert body["query"] == "tasa de paro"
    assert body["include_domains"] == ["ine.es"]
    assert body["max_results"] == 4
    assert body["search_depth"] == "basic"
    assert body["include_raw_content"] == "text"
    assert "country" not in body
    assert (r.url, r.score) == ("https://www.ine.es/a", 0.8)


@pytest.mark.parametrize(("status", "match"), [(401, "TAVILY_API_KEY"), (432, "créditos")])
@respx.mock
def test_tavily_auth_and_quota_errors_stop(status, match):
    respx.post(API_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(EvidenceConfigError, match=match):
        tavily().search("x")


@respx.mock
def test_tavily_bad_request_is_an_evidence_error():
    respx.post(API_URL).mock(return_value=httpx.Response(400))
    with pytest.raises(EvidenceError, match="400"):
        tavily().search("x")


@respx.mock
def test_tavily_retries_rate_limits():
    respx.post(API_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"results": []})]
    )
    assert tavily().search("x").results == []


# --- official source --------------------------------------------------------


@respx.mock
def test_official_source_restricts_domains_filters_score_and_labels_items():
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    result("https://www.ine.es/a", published_date="2026-10-24"),
                    result("https://www.ine.es/flojo", score=0.1),
                ]
            },
        )
    )
    found = OfficialSearchSource(tavily(), DOMAINS).search(claim(), DATE)

    bodies = [json.loads(c.request.content) for c in route.calls]
    assert [b["query"] for b in bodies] == [
        "tasa de paro EPA 2026",
        "parados España 2026",
        "tercera",
    ]
    assert all(set(b["include_domains"]) == set(DOMAINS) for b in bodies)
    [item] = found.items  # same URL from both queries, low score dropped
    assert item.source.kind == "oficial"
    assert item.source.organization == "INE"
    assert item.source.date == dt.date(2026, 10, 24)
    assert item.excerpt == "La tasa de paro se situó en el 10,5 %."


@respx.mock
def test_official_source_falls_back_to_normalized_text_without_queries():
    route = respx.post(API_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    OfficialSearchSource(tavily(), DOMAINS).search(claim([]), DATE)
    assert json.loads(route.calls[0].request.content)["query"] == claim().text_normalized


@respx.mock
def test_official_source_records_failed_query_and_keeps_others():
    respx.post(API_URL).mock(
        side_effect=[
            httpx.Response(400),
            httpx.Response(200, json={"results": [result("https://www.ine.es/a")]}),
            httpx.Response(200, json={"results": []}),
        ]
    )
    found = OfficialSearchSource(tavily(), DOMAINS).search(claim(), DATE)
    assert len(found.items) == 1
    assert "400" in found.errors[0]


# --- web source -------------------------------------------------------------


@respx.mock
def test_web_source_single_query_in_spain_and_labels_official_domains():
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={"results": [result("https://www.ine.es/b"), result("https://periodico.es/n")]},
        )
    )
    found = WebSearchSource(tavily(), DOMAINS).search(claim(), DATE)

    [body] = [json.loads(c.request.content) for c in route.calls]
    assert body["query"] == "tasa de paro EPA 2026"
    assert body["country"] == "spain"
    assert "include_domains" not in body
    kinds = {i.source.url: i.source.kind for i in found.items}
    assert kinds == {"https://www.ine.es/b": "oficial", "https://periodico.es/n": "web"}
    assert found.items[1].source.organization == "periodico.es"


def test_web_source_is_a_fallback_for_official_results():
    assert WebSearchSource(tavily(), DOMAINS).skip_if_found == frozenset({"oficial"})


# --- cascade in the collector ----------------------------------------------


class StubSource:
    def __init__(self, name, kind=None, skip_if_found=frozenset(), fail=False):
        self.name, self.kind, self.skip_if_found, self.fail = name, kind, skip_if_found, fail
        self.calls = 0

    def search(self, claim, speech_date):
        self.calls += 1
        if self.fail:
            return SourceResult(errors=["caída"])
        if self.kind is None:
            return SourceResult()
        url = f"https://{self.name}.es/{self.calls}"
        source = Source(url=url, title="t", organization="o", kind=self.kind)
        return SourceResult(items=[EvidenceItem(id="", source=source, excerpt="x")])


def test_web_runs_only_when_no_official_evidence_was_found():
    official = StubSource("oficial", kind="oficial")
    web = StubSource("web", kind="web", skip_if_found=frozenset({"oficial"}))
    [ev] = collect_evidence([claim()], [official, web], DATE)
    assert web.calls == 0
    assert [i.source.kind for i in ev.items] == ["oficial"]

    empty_official = StubSource("oficial")
    collect_evidence([claim()], [empty_official, web], DATE)
    assert web.calls == 1


def test_failure_check_only_counts_claims_where_the_source_ran():
    official = StubSource("oficial", kind="oficial")
    broken_web = StubSource("web", skip_if_found=frozenset({"oficial"}), fail=True)
    # Web never runs, so it cannot have "failed on every claim".
    collect_evidence([claim(), claim()], [official, broken_web], DATE)
    assert broken_web.calls == 0


@pytest.mark.parametrize(
    "payload",
    [{"detail": {"error": "Invalid"}}, {"results": None}, [], {"error": "x", "results": []}],
)
@respx.mock
def test_tavily_malformed_200_is_an_error(payload):
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(EvidenceError, match="inesperada"):
        tavily().search("x")


@respx.mock
def test_tavily_result_without_score_is_kept_and_bad_entries_are_skipped():
    respx.post(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    result("https://www.ine.es/a", score=None),
                    result("https://www.ine.es/b", score="n/a"),
                    "no es un objeto",
                ]
            },
        )
    )
    found = OfficialSearchSource(tavily(), DOMAINS).search(claim([]), DATE)
    assert {i.source.url for i in found.items} == {"https://www.ine.es/a", "https://www.ine.es/b"}
    assert any("malformado" in e for e in found.errors)


@respx.mock
def test_official_excerpt_includes_relevant_passages_from_the_full_page():
    raw = "\n".join(["Menú de navegación del sitio."] * 100) + (
        "\nLa tasa de paro EPA se situó en el 10,5 % en el tercer trimestre de 2026.\n"
    )
    respx.post(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [result("https://www.ine.es/a", content="Portada EPA", raw_content=raw)]
            },
        )
    )
    [item] = OfficialSearchSource(tavily(), DOMAINS).search(claim(), DATE).items
    assert item.excerpt.startswith("Portada EPA")
    assert "10,5 %" in item.excerpt
    assert "Menú de navegación" not in item.excerpt
