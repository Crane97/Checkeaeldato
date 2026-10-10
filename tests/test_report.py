import datetime as dt

import pytest

from pipeline.models import (
    BlockFailure,
    ClaimDetection,
    CostEntry,
    DetectedClaim,
    RejectedClaim,
    RunCost,
    Source,
    Verdict,
)
from pipeline.report import ReportError, build_report, format_summary

DATE = dt.date(2026, 11, 20)
NOW = dt.datetime(2026, 11, 20, 22, 0, tzinfo=dt.UTC)
INE = Source(url="https://www.ine.es/epa", title="EPA", organization="INE", kind="oficial")
WEB = Source(url="https://periodico.es/n", title="N", organization="periodico.es", kind="web")


def detected(cid: str, speaker: str) -> DetectedClaim:
    return DetectedClaim(
        id=cid,
        speaker=speaker,
        text_original=f"cita {cid}",
        text_normalized=f"Afirmación {cid}.",
        claim_type="estadistica",
        start=1.0,
        end=2.0,
        search_queries=["q"],
    )


def verdict(cid: str, label: str, sources=(), error: str | None = None) -> Verdict:
    return Verdict(
        claim_id=cid,
        verdict=label,
        confidence=0.0 if label == "no_verificable" else 0.8,
        explanation="Explicación.",
        sources=list(sources),
        analysis_error=error,
    )


def build(detection: ClaimDetection, verdicts: list[Verdict], **kwargs):
    args = {
        "job_id": "abc123def456",
        "source": "debate.mp4",
        "input_kind": "video",
        "speech_date": DATE,
        "speakers": {"speaker_0": "Candidata A"},
        "detection": detection,
        "verdicts": verdicts,
        "cost": RunCost(
            entries=[
                CostEntry(step="transcript", provider="elevenlabs", audio_seconds=3600, usd=0.22),
                CostEntry(step="claims", provider="anthropic", model="m", usd=0.01),
                CostEntry(step="verdicts", provider="anthropic", model="m", usd=0.02),
            ]
        ),
        "generated_at": NOW,
    }
    return build_report(**args | kwargs)


def test_merges_claims_and_verdicts_in_order():
    detection = ClaimDetection(
        claims=[detected("c001", "speaker_0"), detected("c002", "speaker_1")]
    )
    report = build(
        detection, [verdict("c002", "no_verificable"), verdict("c001", "verdadero", [INE])]
    )

    assert [c.id for c in report.claims] == ["c001", "c002"]
    first = report.claims[0]
    assert (first.verdict, first.text_original, first.start) == ("verdadero", "cita c001", 1.0)
    assert first.sources == [INE]
    assert report.by_verdict["verdadero"] == 1
    assert report.by_speaker["Candidata A"]["verdadero"] == 1
    assert report.by_basis == {"factcheck": 0, "oficial": 1, "web": 0, "sin_fuente": 1}
    assert report.input_kind == "video"
    assert "search_queries" not in report.model_dump()["claims"][0]


def test_missing_verdict_is_an_error():
    with pytest.raises(ReportError, match="c001"):
        build(ClaimDetection(claims=[detected("c001", "speaker_0")]), [])


def test_warnings_surface_everything_that_went_wrong():
    detection = ClaimDetection(
        claims=[detected("c001", "speaker_0")],
        rejected=[RejectedClaim(quote="x", segment=0, reason="r")] * 2,
        failed_blocks=[BlockFailure(block=3, segments=[7, 8], error="timeout")],
    )
    report = build(detection, [verdict("c001", "no_verificable", error="LLM caído")])
    text = " | ".join(report.warnings)
    assert "1 bloque" in text and "segmentos 7-8" in text
    assert "2 citas descartadas" in text
    assert "1 afirmación sin analizar" in text


def test_summary_shows_totals_speakers_basis_warnings_and_cost():
    detection = ClaimDetection(
        claims=[detected("c001", "speaker_0"), detected("c002", "speaker_1")],
        rejected=[RejectedClaim(quote="x", segment=0, reason="r")],
    )
    report = build(
        detection, [verdict("c001", "verdadero", [WEB]), verdict("c002", "falso", [INE])]
    )

    text = format_summary(report, report_path="data/abc123def456/report.json")

    assert "data/abc123def456/report.json" in text
    assert "Afirmaciones verificables: 2" in text
    assert "Verdadero sin fuente oficial" in text and "Falso con fuente oficial" in text
    assert "No verificable" in text
    assert "Candidata A" in text and "speaker_1" in text
    assert "solo web: 1" in text and "oficial: 1" in text
    assert "1 cita descartada" in text
    assert "0.2500 $" in text
    assert "Claude: 0.0300 $" in text and "transcripción: 0.2200 $" in text


def test_summary_without_claims():
    report = build(ClaimDetection(), [], speakers={})
    assert "Afirmaciones verificables: 0" in format_summary(report, report_path="r.json")


def test_warnings_for_search_errors_and_unpriced_models():
    detection = ClaimDetection(claims=[detected("c001", "speaker_0")])
    v = verdict("c001", "no_verificable").model_copy(update={"evidence_errors": ["web: 500"]})
    cost = RunCost(
        entries=[CostEntry(step="claims", provider="anthropic", model="x", usd=0, priced=False)]
    )
    report = build(detection, [v], cost=cost)
    text = " | ".join(report.warnings)
    assert "1 afirmación con búsquedas de evidencia fallidas" in text
    assert "sin precio" in text and "x" in text


def test_summary_counts_claude_code_as_claude_and_says_it_is_covered_by_the_plan():
    cost = RunCost(entries=[CostEntry(step="claims", provider="claude-code", model="m", usd=0.01)])
    report = build(ClaimDetection(), [], cost=cost)
    text = format_summary(report, report_path="r.json")
    assert "Claude: 0.0100 $ en 1 llamada" in text
    assert "transcripción: 0.0000 $" in text
    assert "cubierto por tu plan" in text
