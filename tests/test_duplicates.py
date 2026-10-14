import datetime as dt

from pipeline.duplicates import mark_duplicates
from pipeline.models import DateRange, DetectedClaim


def claim(cid: str, text: str, speaker: str = "Presidente", **kw) -> DetectedClaim:
    base = {
        "id": cid,
        "speaker": speaker,
        "text_original": text,
        "text_normalized": text,
        "claim_type": "estadistica",
    }
    return DetectedClaim(**base | kw)


def test_repeated_claim_of_the_same_speaker_points_to_the_first():
    claims = mark_duplicates(
        [
            claim("c001", "El salario mínimo ha subido 344 euros al mes desde 2018."),
            claim("c002", "El paro bajó un 10 %."),
            claim("c003", "Desde 2018 el salario mínimo ha subido 344 euros al mes."),
        ]
    )
    assert [c.duplicate_of for c in claims] == [None, None, "c001"]


def test_different_figures_speakers_kinds_or_periods_are_not_grouped():
    base = "El salario mínimo ha subido 344 euros al mes desde 2018."
    period = [DateRange(start=dt.date(2018, 6, 2), end=dt.date(2023, 2, 21))]
    claims = mark_duplicates(
        [
            claim("c001", base),
            claim("c002", base.replace("344", "300")),  # another figure
            claim("c003", base, speaker="Feijóo"),  # another speaker
            claim("c004", base, kind="promesa"),  # a promise is not a claim
            claim("c005", base, context_status="resuelto", time_scope=period),  # other scope
        ]
    )
    assert [c.duplicate_of for c in claims] == [None] * 5


def test_a_duplicate_of_a_duplicate_points_to_the_first_one():
    text = "La deuda pública supera el 100 % del PIB."
    claims = mark_duplicates([claim("c001", text), claim("c002", text), claim("c003", text)])
    assert [c.duplicate_of for c in claims] == [None, "c001", "c001"]


def test_repetitions_get_a_copy_of_the_original_result():
    from pipeline.duplicates import with_repetitions
    from pipeline.models import Verdict

    claims = mark_duplicates(
        [
            claim("c001", "La deuda supera el 100 % del PIB."),
            claim("c002", "La deuda supera el 100 % del PIB."),
        ]
    )
    [original] = [Verdict(claim_id="c001", verdict="no_verificable", confidence=0, explanation="x")]
    out = with_repetitions(claims, [original])
    assert [(v.claim_id, v.explanation) for v in out] == [("c001", "x"), ("c002", "x")]
