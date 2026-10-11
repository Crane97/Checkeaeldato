import httpx
import respx

from pipeline.evidence.speaker_search import TavilySpeakerSearch
from pipeline.evidence.tavily import API_URL, TavilyClient

DOMAINS = {"congreso.es": "Congreso de los Diputados"}
PAGE = {
    "title": "Ficha",
    "url": "https://www.congreso.es/gil",
    "content": "Ana Gil Ruiz, Grupo Parlamentario Popular.",
    "score": 0.8,
    "raw_content": "Ana Gil Ruiz. Grupo Parlamentario Popular. Presidenta del Partido Popular.",
}


def make(handler) -> TavilySpeakerSearch:
    client = TavilyClient("k", client=httpx.Client(), backoff=0)
    respx.post(API_URL).mock(side_effect=handler)
    return TavilySpeakerSearch(client, domains=DOMAINS)


@respx.mock
def test_official_search_is_restricted_to_official_domains():
    bodies = []

    def handler(request):
        bodies.append(request.read())
        return httpx.Response(200, json={"results": [PAGE]})

    items, errors = make(handler)("Gil Ruiz partido político cargo 2023", official=True)
    assert b"congreso.es" in bodies[0]
    assert [i.source.kind for i in items] == ["oficial"]
    assert "Presidenta del Partido Popular" in items[0].excerpt
    assert errors == []


@respx.mock
def test_web_search_is_not_restricted_and_errors_are_returned():
    items, errors = make(lambda r: httpx.Response(500))("Gil partido", official=False)
    assert items == [] and errors and "Gil partido" in errors[0]
