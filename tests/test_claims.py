import datetime as dt
from typing import Any

import pytest

from pipeline.claims import (
    PROMPT,
    ClaimsError,
    DetectionResult,
    ExtractedClaim,
    build_blocks,
    detect_claims,
    format_block,
    merge_speaker_aliases,
)
from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import Segment, Transcript

DATE = dt.date(2026, 11, 20)
MODEL = "claude-haiku-5-5"


def video_transcript(*texts: str) -> Transcript:
    segments = [
        Segment(speaker=f"speaker_{i % 2}", start=i * 10.0, end=i * 10.0 + 9.5, text=t)
        for i, t in enumerate(texts)
    ]
    return Transcript(provider="elevenlabs", language="es", duration=600, segments=segments)


def text_transcript(*texts: str) -> Transcript:
    return Transcript(
        provider="texto",
        language="es",
        segments=[Segment(speaker="autor", text=t) for t in texts],
    )


def extracted(segment: int, quote: str, **overrides: Any) -> ExtractedClaim:
    data = {
        "segment": segment,
        "quote": quote,
        "text_normalized": f"Normalizada: {quote}",
        "claim_type": "estadistica",
        "search_queries": ["paro EPA 2026"],
        "speaker": "",
        "needs_context": False,
    }
    return ExtractedClaim(**data | overrides)


class FakeLLM:
    """Returns queued results (or raises queued exceptions) in call order."""

    def __init__(self, *results: DetectionResult | Exception) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def structured(self, **kwargs: Any) -> DetectionResult:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


# --- build_blocks -----------------------------------------------------------


def test_short_transcript_is_one_block_without_context():
    segments = video_transcript("a" * 100, "b" * 100).segments
    [block] = build_blocks(segments, core_chars=1000, context_chars=300)
    assert (block.before, block.core, block.after) == ([], [0, 1], [])


def test_every_segment_is_core_exactly_once_and_context_is_bounded():
    segments = video_transcript(*["x" * 400] * 10).segments
    blocks = build_blocks(segments, core_chars=1000, context_chars=500)

    cores = [i for b in blocks for i in b.core]
    assert cores == list(range(10))
    for b in blocks:
        assert sum(len(segments[i].text) for i in b.before) <= 500 or len(b.before) == 1
        assert all(i < b.core[0] for i in b.before)
        assert all(i > b.core[-1] for i in b.after)
    assert blocks[1].before == [blocks[0].core[-1]]
    assert blocks[0].after == [blocks[1].core[0]]


def test_oversized_segment_gets_its_own_block():
    segments = video_transcript("a" * 50, "b" * 5000, "c" * 50).segments
    blocks = build_blocks(segments, core_chars=1000, context_chars=100)
    assert [1] in [b.core for b in blocks]


def test_empty_transcript_has_no_blocks():
    assert build_blocks([]) == []


# --- format_block -----------------------------------------------------------


def test_format_block_marks_sections_segments_speakers_and_times():
    transcript = video_transcript("antes", "centro", "después")
    blocks = build_blocks(transcript.segments, core_chars=5, context_chars=100)
    content = format_block(transcript, blocks[1], DATE, "video")

    assert "<fecha>2026-11-20</fecha>" in content
    assert "<tipo_entrada>video</tipo_entrada>" in content
    assert "<contexto_anterior>\n[S0] speaker_0 (00:00): antes\n</contexto_anterior>" in content
    assert "<fragmento>\n[S1] speaker_1 (00:10): centro\n</fragmento>" in content
    assert "[S2] speaker_0 (00:20): después" in content


def test_format_block_for_text_has_no_times_or_empty_sections():
    transcript = text_transcript("Un párrafo.")
    [block] = build_blocks(transcript.segments)
    content = format_block(transcript, block, DATE, "texto")
    assert "<fragmento>\n[S0] autor: Un párrafo.\n</fragmento>" in content
    assert "contexto_anterior" not in content


# --- detect_claims ----------------------------------------------------------


def run(transcript: Transcript, llm: FakeLLM, kind: str = "video", **kwargs: Any):
    return detect_claims(
        transcript, speech_date=DATE, input_kind=kind, llm=llm, model=MODEL, **kwargs
    )


def test_claims_get_speaker_times_and_sequential_ids():
    transcript = video_transcript("Hola a todos.", "El paro ha bajado un 20 % este año.")
    llm = FakeLLM(DetectionResult(claims=[extracted(1, "El paro ha bajado un 20 %")]))

    result = run(transcript, llm)

    [claim] = result.claims
    assert claim.id == "c001"
    assert claim.speaker == "speaker_1"
    assert (claim.start, claim.end) == (10.0, 19.5)
    assert claim.text_original == "El paro ha bajado un 20 %"
    assert claim.search_queries == ["paro EPA 2026"]
    assert llm.calls[0]["prompt"] == PROMPT
    assert llm.calls[0]["model"] == MODEL
    assert llm.calls[0]["step"] == "claims"


def test_text_claims_have_no_times():
    transcript = text_transcript("La deuda pública subió al 110 % del PIB.")
    llm = FakeLLM(DetectionResult(claims=[extracted(0, "La deuda pública subió al 110 % del PIB")]))
    [claim] = run(transcript, llm, kind="texto").claims
    assert (claim.start, claim.end, claim.speaker) == (None, None, "autor")


def test_quote_matching_tolerates_spacing_case_and_quote_marks():
    transcript = text_transcript("Dijo:  «el paro bajó»   ayer.")
    llm = FakeLLM(DetectionResult(claims=[extracted(0, 'dijo: "El paro bajó" ayer')]))
    assert len(run(transcript, llm, kind="texto").claims) == 1


def test_paraphrased_quote_is_rejected_and_recorded():
    transcript = text_transcript("El paro ha bajado un 20 % este año.")
    llm = FakeLLM(DetectionResult(claims=[extracted(0, "El desempleo cayó un 20 %")]))

    result = run(transcript, llm, kind="texto")

    assert result.claims == []
    [rejected] = result.rejected
    assert rejected.quote == "El desempleo cayó un 20 %"
    assert "literal" in rejected.reason


def test_quote_spanning_two_segments_of_same_speaker_takes_both_times():
    transcript = video_transcript("El paro ha bajado", "un 20 % este año.")
    for segment in transcript.segments:
        segment.speaker = "speaker_0"
    llm = FakeLLM(DetectionResult(claims=[extracted(0, "El paro ha bajado un 20 %")]))
    [claim] = run(transcript, llm).claims
    assert (claim.start, claim.end) == (0.0, 19.5)


def test_quote_cannot_span_into_another_speaker():
    transcript = video_transcript("El paro ha bajado", "un 20 % este año.")
    llm = FakeLLM(DetectionResult(claims=[extracted(0, "El paro ha bajado un 20 %")]))
    result = run(transcript, llm)
    assert result.claims == []
    assert len(result.rejected) == 1


def test_unknown_segment_is_rejected():
    transcript = video_transcript("uno con 10 %.")
    llm = FakeLLM(DetectionResult(claims=[extracted(0, "uno con 10 %"), extracted(9, "x")]))
    result = run(transcript, llm)
    assert [c.text_original for c in result.claims] == ["uno con 10 %"]
    [rejected] = result.rejected
    assert (rejected.segment, rejected.reason) == (9, "segmento inexistente")


def test_context_claims_are_not_duplicated_across_blocks():
    transcript = video_transcript("uno con 10 %.", "dos con 20 %.")
    same = DetectionResult(claims=[extracted(0, "uno con 10 %"), extracted(1, "dos con 20 %")])
    result = run(transcript, FakeLLM(same, same), core_chars=5, context_chars=100)
    assert [c.text_original for c in result.claims] == ["uno con 10 %", "dos con 20 %"]
    assert [c.id for c in result.claims] == ["c001", "c002"]


def test_failed_block_is_recorded_and_others_continue():
    transcript = video_transcript("uno con 10 %.", "dos con 20 %.")
    llm = FakeLLM(
        LLMError("respuesta inválida"),
        DetectionResult(claims=[extracted(1, "dos con 20 %")]),
    )
    result = run(transcript, llm, core_chars=5, context_chars=0)

    assert [c.text_original for c in result.claims] == ["dos con 20 %"]
    [failure] = result.failed_blocks
    assert (failure.block, failure.segments) == (0, [0])
    assert "respuesta inválida" in failure.error


def test_all_blocks_failing_stops_the_step():
    transcript = video_transcript("uno.", "dos.")
    llm = FakeLLM(LLMError("a"), LLMError("b"))
    with pytest.raises(ClaimsError, match="Todos los bloques"):
        run(transcript, llm, core_chars=2, context_chars=0)


def test_config_error_stops_immediately():
    transcript = video_transcript("uno.", "dos.")
    llm = FakeLLM(LLMConfigError("clave inválida"))
    with pytest.raises(LLMConfigError):
        run(transcript, llm, core_chars=2, context_chars=0)
    assert len(llm.calls) == 1


def test_search_queries_are_capped():
    transcript = text_transcript("El paro bajó un 20 %.")
    many = extracted(0, "El paro bajó un 20 %", search_queries=["a", "b", "c", "d", "e", " "])
    [claim] = run(transcript, FakeLLM(DetectionResult(claims=[many])), kind="texto").claims
    assert claim.search_queries == ["a", "b", "c", "d"]


# --- speakers ---------------------------------------------------------------


def test_prose_claims_are_attributed_to_the_person_the_text_names():
    transcript = text_transcript("La ministra de Hacienda afirmó que la deuda bajó al 99 %.")
    item = extracted(0, "la deuda bajó al 99 %", speaker="Ministra de Hacienda")
    [claim] = run(transcript, FakeLLM(DetectionResult(claims=[item])), kind="texto").claims
    assert claim.speaker == "Ministra de Hacienda"


def test_author_claims_without_a_named_speaker_stay_with_the_author():
    transcript = text_transcript("Según el INE, la población supera los 49 millones.")
    item = extracted(0, "la población supera los 49 millones", speaker="")
    [claim] = run(transcript, FakeLLM(DetectionResult(claims=[item])), kind="texto").claims
    assert claim.speaker == "autor"


def test_dialogue_labels_win_over_reported_speech():
    transcript = Transcript(
        provider="texto",
        language="es",
        segments=[Segment(speaker="Candidata A", text="Usted dijo que el paro bajó un 30 %.")],
    )
    item = extracted(0, "el paro bajó un 30 %", speaker="Candidato B")
    [claim] = run(transcript, FakeLLM(DetectionResult(claims=[item])), kind="texto").claims
    assert claim.speaker == "Candidata A"


def test_video_keeps_diarized_speakers():
    transcript = video_transcript("El paro bajó un 15 %.")
    item = extracted(0, "El paro bajó un 15 %", speaker="Ana")
    [claim] = run(transcript, FakeLLM(DetectionResult(claims=[item]))).claims
    assert claim.speaker == "speaker_0"


def test_name_variants_are_merged_into_the_full_name():
    transcript = text_transcript("Sánchez dijo A con 10 %.", "Pedro Sánchez dijo B con 20 %.")
    llm = FakeLLM(
        DetectionResult(
            claims=[
                extracted(0, "A con 10 %", speaker="Sánchez"),
                extracted(1, "B con 20 %", speaker="Pedro Sánchez"),
            ]
        )
    )
    claims = run(transcript, llm, kind="texto").claims
    assert [c.speaker for c in claims] == ["Pedro Sánchez", "Pedro Sánchez"]


def test_alias_merging_rules():
    assert merge_speaker_aliases(["Sánchez", "Pedro Sánchez", "autor"]) == {
        "Sánchez": "Pedro Sánchez"
    }
    # Ambiguous: "García" could be either person, so it is left alone.
    assert merge_speaker_aliases(["García", "Ana García", "Luis García"]) == {}
    assert merge_speaker_aliases(["Candidata A", "Candidata B"]) == {}
    assert merge_speaker_aliases(["MINISTRA", "Ministra de Hacienda"]) == {
        "MINISTRA": "Ministra de Hacienda"
    }


# --- context flag -----------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "flag", "expected"),
    [
        ("cuando ustedes gobernaban el paro superó el 26 %", False, True),  # caught by code
        ("este Gobierno ha subido los impuestos un 10 %", False, True),
        ("hace diez años el paro era del 26 %", False, True),
        ("la deuda subió con su gobierno un 10 %", False, True),
        ("La deuda pública supera el 100 % del PIB", False, False),
        ("La deuda pública supera el 100 % del PIB", True, True),  # the model can flag it too
    ],
)
def test_claims_with_implicit_actors_or_periods_need_context(quote, flag, expected):
    transcript = text_transcript(quote + ".")
    item = extracted(0, quote, needs_context=flag)
    [claim] = run(transcript, FakeLLM(DetectionResult(claims=[item])), kind="texto").claims
    assert claim.needs_context is expected
    assert claim.segment == 0


def run_detect(transcript: Transcript, item: ExtractedClaim):
    llm = FakeLLM(DetectionResult(claims=[item]))
    return detect_claims(transcript, speech_date=DATE, input_kind="video", llm=llm, model=MODEL)


def test_promises_are_kept_with_their_kind():
    transcript = video_transcript("Bajaremos el paro al 0 % en dos años.")
    item = extracted(0, "Bajaremos el paro al 0 % en dos años", kind="promesa")
    assert [c.kind for c in run_detect(transcript, item).claims] == ["promesa"]


def test_claims_are_statements_by_default():
    transcript = video_transcript("El paro bajó un 15 %.")
    detection = run_detect(transcript, extracted(0, "El paro bajó un 15 %"))
    assert detection.claims[0].kind == "afirmacion"
