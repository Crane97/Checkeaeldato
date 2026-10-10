import datetime as dt
from pathlib import Path

import pytest

from pipeline.evaluation import (
    GOLD_DIR,
    GoldCase,
    GoldClaim,
    load_gold_cases,
    match_claims,
    score_case,
    summarize,
)
from pipeline.models import Claim, Report, RunCost, Source

DATE = dt.date(2026, 11, 20)
INE = Source(url="https://www.ine.es/x", title="t", organization="INE", kind="oficial")


def claim(cid: str, quote: str, verdict: str = "no_verificable", start=None, end=None) -> Claim:
    return Claim(
        id=cid,
        speaker="autor",
        text_original=quote,
        text_normalized=quote,
        claim_type="estadistica",
        start=start,
        end=end,
        verdict=verdict,
        confidence=0.0,
        explanation="x",
        sources=[] if verdict == "no_verificable" else [INE],
    )


def report(*claims: Claim) -> Report:
    return Report(
        job_id="j",
        source="s.txt",
        input_kind="texto",
        speech_date=DATE,
        generated_at=dt.datetime(2026, 11, 20, tzinfo=dt.UTC),
        claims=list(claims),
        cost=RunCost(),
    )


def gold(*claims: GoldClaim, not_claims: list[str] | None = None) -> GoldCase:
    return GoldCase(
        id="caso",
        descripcion="d",
        fecha=DATE,
        entrada={"tipo": "texto", "texto": "irrelevante"},
        afirmaciones=list(claims),
        no_afirmaciones=not_claims or [],
    )


def test_matching_tolerates_small_wording_differences():
    g = [GoldClaim(cita="El paro ha bajado un 20 % este año", veredicto_esperado="falso")]
    d = [claim("c1", "el paro ha bajado un 20%"), claim("c2", "La deuda subió")]
    assert match_claims(g, d) == {0: "c1"}


def test_matching_is_one_to_one_and_uses_time_when_available():
    g = [
        GoldClaim(cita="el paro bajó", inicio=100, fin=105, veredicto_esperado="falso"),
        GoldClaim(cita="el paro bajó", inicio=300, fin=305, veredicto_esperado="verdadero"),
    ]
    d = [
        claim("c1", "el paro bajó", start=301, end=304),
        claim("c2", "el paro bajó", start=99, end=106),
    ]
    assert match_claims(g, d) == {0: "c2", 1: "c1"}


def test_score_case_counts_coverage_verdicts_and_false_positives():
    case = gold(
        GoldClaim(cita="El paro ha bajado un 20 %", veredicto_esperado="enganoso"),
        GoldClaim(cita="La deuda es del 105 % del PIB", veredicto_esperado="verdadero"),
        not_claims=["Vamos a crear un millón de empleos"],
    )
    result = score_case(
        case,
        report(
            claim("c1", "El paro ha bajado un 20 %", "falso"),
            claim("c2", "vamos a crear un millón de empleos", "no_verificable"),
            claim("c3", "Otra cosa distinta", "verdadero"),
        ),
    )
    assert (result.gold_claims, result.matched, result.correct) == (2, 1, 0)
    assert result.false_positives == 1
    assert result.extra_detected == 1  # c3: not in gold, not a known opinion
    assert result.confusion == {("enganoso", "falso"): 1}
    assert result.missed == ["La deuda es del 105 % del PIB"]


def test_summarize_aggregates_rates():
    case = gold(GoldClaim(cita="a b c d", veredicto_esperado="falso"))
    r1 = score_case(case, report(claim("c1", "a b c d", "falso")))
    r2 = score_case(case, report())
    summary = summarize([r1, r2])
    assert summary.coverage == pytest.approx(0.5)
    assert summary.verdict_accuracy == pytest.approx(1.0)
    assert summary.cases == 2


def test_summarize_handles_no_matches():
    summary = summarize([score_case(gold(), report())])
    assert summary.coverage is None and summary.verdict_accuracy is None


def test_bundled_gold_cases_are_valid():
    cases = load_gold_cases(GOLD_DIR)
    assert cases, "debe haber al menos un caso de ejemplo"
    assert all(isinstance(c, GoldCase) for c in cases)


def test_invalid_gold_case_reports_file(tmp_path: Path):
    (tmp_path / "malo.json").write_text('{"id": "x"}')
    with pytest.raises(ValueError, match="malo.json"):
        load_gold_cases(tmp_path)
