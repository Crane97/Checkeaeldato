import datetime as dt

import pytest
from pydantic import ValidationError

from pipeline.models import (
    Claim,
    CostEntry,
    Report,
    RunCost,
    Segment,
    Source,
    Verdict,
)

FACTCHECK = Source(
    url="https://maldita.es/malditobulo/ejemplo",
    title="Ejemplo de desmentido",
    organization="Maldita",
    date=dt.date(2026, 9, 1),
    kind="factcheck",
)
OFFICIAL = Source(
    url="https://www.ine.es/ejemplo",
    title="Encuesta de Población Activa",
    organization="INE",
    kind="oficial",
)


def make_claim(**overrides) -> Claim:
    data = {
        "id": "c001",
        "speaker": "speaker_0",
        "start": 10.0,
        "end": 15.5,
        "text_original": "el paro ha bajado un 20 %",
        "text_normalized": "El paro en España bajó un 20 % entre 2024 y 2026.",
        "claim_type": "estadistica",
        "verdict": "verdadero",
        "confidence": 0.8,
        "explanation": "Los datos de la EPA muestran un descenso similar.",
        "sources": [OFFICIAL],
    }
    return Claim(**data | overrides)


def test_valid_claim_roundtrips_through_json():
    claim = make_claim()
    assert Claim.model_validate_json(claim.model_dump_json()) == claim


def test_verdict_other_than_no_verificable_requires_source():
    with pytest.raises(ValidationError, match="at least one source"):
        make_claim(sources=[])


def test_no_verificable_allows_no_sources():
    claim = make_claim(verdict="no_verificable", sources=[], confidence=0.0)
    assert claim.sources == []


def test_matched_factcheck_must_be_a_cited_factcheck_source():
    with pytest.raises(ValidationError, match="matched_factcheck"):
        make_claim(verdict="falso", matched_factcheck=OFFICIAL.url)

    claim = make_claim(verdict="falso", sources=[FACTCHECK], matched_factcheck=FACTCHECK.url)
    assert claim.matched_factcheck == FACTCHECK.url


@pytest.mark.parametrize("word", ["mentira", "Miente", "MIENTEN", "mintió"])
def test_explanation_rejects_forbidden_wording(word):
    with pytest.raises(ValidationError, match="forbidden wording"):
        make_claim(explanation=f"Esta afirmación es una {word}.")


def test_explanation_allows_words_containing_forbidden_substrings():
    # "mientras" shares a prefix with "miente" but is not a forbidden word.
    make_claim(explanation="El paro bajó mientras crecía la población activa.")


@pytest.mark.parametrize(("start", "end"), [(-1.0, 2.0), (5.0, 4.0)])
def test_invalid_time_ranges_are_rejected(start, end):
    with pytest.raises(ValidationError):
        Segment(speaker="speaker_0", start=start, end=end, text="hola")


def test_confidence_must_be_between_0_and_1():
    with pytest.raises(ValidationError):
        make_claim(confidence=1.5)


def test_source_url_must_be_http():
    with pytest.raises(ValidationError):
        Source(url="ftp://x", title="t", organization="o", kind="web")


def test_verdict_model_applies_same_rules():
    with pytest.raises(ValidationError):
        Verdict(claim_id="c001", verdict="falso", confidence=0.9, explanation="x")


def test_report_counts_by_verdict_and_mapped_speaker():
    report = Report(
        job_id="abc123",
        source="debate.mp4",
        input_kind="video",
        speech_date=dt.date(2026, 11, 20),
        generated_at=dt.datetime(2026, 11, 20, 22, 0),
        speakers={"speaker_0": "Candidata A"},
        claims=[
            make_claim(id="c1"),
            make_claim(id="c2", verdict="no_verificable", sources=[]),
            make_claim(id="c3", speaker="speaker_1", verdict="falso"),
        ],
        cost=RunCost(
            entries=[
                CostEntry(step="transcript", provider="elevenlabs", audio_seconds=3600, usd=0.22),
                CostEntry(step="claims", provider="anthropic", input_tokens=1000, usd=0.015),
            ]
        ),
    )

    assert report.by_verdict == {"verdadero": 1, "falso": 1, "no_verificable": 1}
    assert report.by_category == {
        "verdadero": 1,
        "verdadero_sin_fuente_oficial": 0,
        "no_verificable": 1,
        "falso_sin_fuente_oficial": 0,
        "falso_con_fuente_oficial": 1,
    }
    assert report.by_speaker["Candidata A"]["verdadero"] == 1  # categories
    assert report.by_speaker["Candidata A"]["no_verificable"] == 1
    assert report.by_speaker["speaker_1"]["falso_con_fuente_oficial"] == 1
    assert report.cost.total_usd == pytest.approx(0.235)

    dumped = report.model_dump(mode="json")
    assert dumped["by_verdict"]["verdadero"] == 1
    assert Report.model_validate(dumped).claims == report.claims


def test_text_claim_has_no_times():
    claim = make_claim(start=None, end=None)
    assert claim.start is None and claim.end is None


@pytest.mark.parametrize(("start", "end"), [(10.0, None), (None, 15.0)])
def test_claim_times_go_together(start, end):
    with pytest.raises(ValidationError, match="both"):
        make_claim(start=start, end=end)


def test_text_segment_has_no_times():
    segment = Segment(speaker="autor", text="El paro bajó.")
    assert segment.start is None


def test_basis_is_the_strongest_cited_source_kind():
    web = Source(url="https://periodico.es/n", title="t", organization="o", kind="web")
    assert make_claim(sources=[web, OFFICIAL]).basis == "oficial"
    assert make_claim(sources=[web]).basis == "web"
    assert make_claim(verdict="falso", sources=[web, FACTCHECK]).basis == "factcheck"
    assert make_claim(verdict="no_verificable", sources=[]).basis is None
    assert make_claim().model_dump(mode="json")["basis"] == "oficial"


@pytest.mark.parametrize(
    ("verdict", "kinds", "category"),
    [
        ("verdadero", ["oficial"], "verdadero"),
        ("verdadero", ["factcheck"], "verdadero"),  # professional fact-checkers count as reliable
        ("verdadero", ["web"], "verdadero_sin_fuente_oficial"),
        ("verdadero", ["web", "oficial"], "verdadero"),
        ("falso", ["oficial"], "falso_con_fuente_oficial"),
        ("falso", ["factcheck"], "falso_con_fuente_oficial"),
        ("falso", ["web"], "falso_sin_fuente_oficial"),
        ("no_verificable", [], "no_verificable"),
        ("no_verificable", ["oficial"], "no_verificable"),
    ],
)
def test_category_combines_verdict_and_source_reliability(verdict, kinds, category):
    sources = [
        Source(url=f"https://x{i}.es", title="t", organization="o", kind=k)
        for i, k in enumerate(kinds)
    ]
    assert make_claim(verdict=verdict, sources=sources).category == category


def test_enganoso_is_no_longer_a_verdict():
    with pytest.raises(ValidationError):
        make_claim(verdict="enganoso")
