import datetime as dt

import pytest

from pipeline.evidence.base import (
    EvidenceConfigError,
    EvidenceError,
    SourceResult,
    collect_evidence,
    settled_by_factcheck,
)
from pipeline.models import DetectedClaim, EvidenceItem, Source

DATE = dt.date(2026, 11, 20)


def claim(cid: str) -> DetectedClaim:
    return DetectedClaim(
        id=cid,
        speaker="autor",
        text_original="El paro bajó un 20 %",
        text_normalized="El paro en España bajó un 20 %.",
        claim_type="estadistica",
    )


def item(url: str, kind: str = "factcheck") -> EvidenceItem:
    return EvidenceItem(
        id="",
        source=Source(url=url, title="t", organization="o", kind=kind),
        excerpt="extracto",
    )


class FakeSource:
    def __init__(self, name: str, results: dict[str, SourceResult | Exception]) -> None:
        self.name = name
        self.results = results
        self.calls: list[str] = []

    def search(self, claim: DetectedClaim, speech_date: dt.date) -> SourceResult:
        self.calls.append(claim.id)
        result = self.results.get(claim.id, SourceResult())
        if isinstance(result, Exception):
            raise result
        return result


def test_items_are_numbered_per_claim_and_deduplicated_by_url():
    first = FakeSource(
        "factcheck",
        {
            "c1": SourceResult(items=[item("https://a.es/1"), item("https://a.es/2")]),
            "c2": SourceResult(items=[item("https://a.es/3")]),
        },
    )
    second = FakeSource(
        "oficial",
        {"c1": SourceResult(items=[item("https://a.es/2", "oficial"), item("https://ine.es/x")])},
    )

    result = collect_evidence([claim("c1"), claim("c2")], [first, second], DATE)

    assert [e.claim_id for e in result] == ["c1", "c2"]
    assert [(i.id, i.source.url) for i in result[0].items] == [
        ("E1", "https://a.es/1"),
        ("E2", "https://a.es/2"),
        ("E3", "https://ine.es/x"),
    ]
    assert result[0].items[1].source.kind == "factcheck"  # first source wins on duplicates
    assert [i.id for i in result[1].items] == ["E1"]


def test_partial_and_failed_searches_are_recorded_with_source_name():
    source = FakeSource(
        "factcheck",
        {
            "c1": SourceResult(items=[item("https://a.es/1")], errors=["consulta 2: 500"]),
            "c2": EvidenceError("timeout"),
        },
    )
    other = FakeSource("oficial", {"c2": SourceResult(items=[item("https://ine.es/x")])})

    c1, c2 = collect_evidence([claim("c1"), claim("c2")], [source, other], DATE)

    assert c1.errors == ["factcheck: consulta 2: 500"]
    assert c2.errors == ["factcheck: timeout"]
    assert [i.source.url for i in c2.items] == ["https://ine.es/x"]
    assert other.calls == ["c1", "c2"]


def test_unexpected_exception_is_recorded_not_raised():
    source = FakeSource(
        "factcheck",
        {"c1": SourceResult(items=[item("https://a.es/1")]), "c2": KeyError("claims")},
    )
    _, c2 = collect_evidence([claim("c1"), claim("c2")], [source], DATE)
    assert c2.errors == ["factcheck: error inesperado KeyError: 'claims'"]


def test_config_error_stops_collection():
    source = FakeSource("factcheck", {"c1": EvidenceConfigError("clave inválida")})
    with pytest.raises(EvidenceConfigError):
        collect_evidence([claim("c1"), claim("c2")], [source], DATE)
    assert source.calls == ["c1"]


def test_source_failing_on_every_claim_stops_collection():
    source = FakeSource(
        "factcheck",
        {
            "c1": EvidenceError("caída"),
            "c2": SourceResult(errors=["500"]),
            "c3": EvidenceError("caída"),
        },
    )
    with pytest.raises(EvidenceError, match="factcheck falló en todas"):
        collect_evidence([claim("c1"), claim("c2"), claim("c3")], [source], DATE)


def test_few_failures_are_recorded_without_stopping():
    # With fewer than 3 searches a transient error is not proof the source is down.
    source = FakeSource("factcheck", {"c1": EvidenceError("HTTP 500")})
    [ev] = collect_evidence([claim("c1")], [source], DATE)
    assert ev.errors == ["factcheck: HTTP 500"]


def test_no_claims_means_no_evidence():
    assert collect_evidence([], [FakeSource("factcheck", {})], DATE) == []


def reviewed(text: str) -> EvidenceItem:
    return item("https://maldita.es/x").model_copy(update={"claim_reviewed": text})


@pytest.mark.parametrize(
    ("claim_reviewed", "settled"),
    [
        ("El paro en España bajó un 20 %", True),
        ("Según el Gobierno, el paro bajó un 20 % en España", True),
        ("El paro bajó un 15 %", False),  # another figure: another claim
        ("La deuda pública supera el 100 % del PIB", False),
        (None, False),
    ],
)
def test_settled_by_factcheck_needs_a_similar_claim_with_the_same_figures(claim_reviewed, settled):
    items = [reviewed(claim_reviewed) if claim_reviewed else item("https://maldita.es/x")]
    assert settled_by_factcheck(claim("c1"), items) is settled


def test_web_sources_are_skipped_when_a_published_check_matches():
    factcheck = FakeSource(
        "factcheck", {"c1": SourceResult(items=[reviewed("El paro en España bajó un 20 %")])}
    )
    oficial, web = FakeSource("oficial", {}), FakeSource("web", {})
    [result] = collect_evidence([claim("c1")], [factcheck, oficial, web], DATE)
    assert oficial.calls == [] and web.calls == []
    assert [i.source.kind for i in result.items] == ["factcheck"]


def test_web_sources_still_run_when_the_check_is_about_another_figure():
    factcheck = FakeSource(
        "factcheck", {"c1": SourceResult(items=[reviewed("El paro bajó un 15 %")])}
    )
    oficial = FakeSource("oficial", {})
    collect_evidence([claim("c1")], [factcheck, oficial], DATE)
    assert oficial.calls == ["c1"]
