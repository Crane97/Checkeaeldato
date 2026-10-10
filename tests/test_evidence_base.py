import datetime as dt

import pytest

from pipeline.evidence.base import (
    EvidenceConfigError,
    EvidenceError,
    SourceResult,
    collect_evidence,
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
        "factcheck", {"c1": EvidenceError("caída"), "c2": SourceResult(errors=["500"])}
    )
    with pytest.raises(EvidenceError, match="factcheck falló en todas"):
        collect_evidence([claim("c1"), claim("c2")], [source], DATE)


def test_no_claims_means_no_evidence():
    assert collect_evidence([], [FakeSource("factcheck", {})], DATE) == []
