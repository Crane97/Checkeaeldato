import datetime as dt
from typing import Any

import pytest

from pipeline.context import PROMPT, ContextAnswer, resolve_context
from pipeline.governments import default_governments
from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import (
    ClaimDetection,
    DetectedClaim,
    Segment,
    Speaker,
    SpeakerMap,
    Transcript,
)

DATE = dt.date(2023, 7, 10)
D = dt.date
MODEL = "claude-opus-5-5"

SPEAKERS = SpeakerMap(
    speakers=[
        Speaker(label="SPEAKER_0", role="moderadora", confidence=0.8),
        Speaker(label="SPEAKER_1", name="Ana Ruiz", party="PSOE", confidence=0.9),
        Speaker(label="SPEAKER_2", name="Luis Gómez", party="PP", confidence=0.9),
        Speaker(label="SPEAKER_3"),
    ]
)
TRANSCRIPT = Transcript(
    provider="elevenlabs",
    language="es",
    duration=60,
    segments=[
        Segment(speaker="SPEAKER_0", text="Empezamos con el empleo.", start=0, end=5),
        Segment(speaker="SPEAKER_2", text="Nosotros creamos empleo.", start=5, end=10),
        Segment(
            speaker="SPEAKER_1",
            text="Señor Gómez, cuando ustedes gobernaban el paro superó el 26 %.",
            start=10,
            end=15,
        ),
    ],
)


def claim(quote: str, speaker: str = "SPEAKER_1", needs_context: bool = True) -> DetectedClaim:
    return DetectedClaim(
        id="c001",
        speaker=speaker,
        segment=2,
        text_original=quote,
        text_normalized=f"{quote[0].upper()}{quote[1:]}.",
        claim_type="estadistica",
        search_queries=["tasa de paro EPA máximo histórico"],
        needs_context=needs_context,
    )


def answer(**fields: Any) -> ContextAnswer:
    data = {
        "referent": "interlocutor",
        "addressee": "SPEAKER_2",
        "party": "",
        "president": "",
        "current_term_only": False,
        "years": [],
        "years_ago": None,
        "confidence": 0.9,
        "notes": "«Ustedes» se dirige al señor Gómez, del partido del interlocutor.",
        "rewritten": (
            "Durante los gobiernos de {actor} ({periodo}), la tasa de paro superó el 26 %."
        ),
    }
    return ContextAnswer(**data | fields)


class FakeLLM:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def resolve(c: DetectedClaim, llm: FakeLLM, speakers: SpeakerMap = SPEAKERS) -> DetectedClaim:
    [resolved] = resolve_context(
        ClaimDetection(claims=[c]),
        TRANSCRIPT,
        speakers,
        speech_date=DATE,
        llm=llm,
        model=MODEL,
        governments=default_governments(),
    ).claims
    return resolved


def scope(c: DetectedClaim) -> list[tuple[dt.date, dt.date]]:
    return [(r.start, r.end) for r in c.time_scope]


def test_ustedes_from_a_socialist_speaker_to_a_popular_one():
    llm = FakeLLM(answer())
    c = resolve(claim("cuando ustedes gobernaban el paro superó el 26 %"), llm)

    assert c.context_status == "resuelto"
    assert c.resolved_actor.startswith("PP")
    assert scope(c) == [(D(1996, 5, 5), D(2004, 4, 17)), (D(2011, 12, 21), D(2018, 6, 2))]
    assert c.text_normalized.startswith("Durante los gobiernos de PP")
    assert "05/05/1996" in c.text_normalized and "02/06/2018" in c.text_normalized
    assert any("1996" in q for q in c.search_queries) and any("2018" in q for q in c.search_queries)
    assert c.resolution_confidence == 0.9
    assert "Gómez" in c.resolution_notes
    assert llm.calls[0]["prompt"] == PROMPT and llm.calls[0]["model"] == MODEL
    content = llm.calls[0]["user_content"]
    assert "Ana Ruiz (PSOE)" in content and "Señor Gómez" in content


def test_president_named_in_the_sentence():
    llm = FakeLLM(
        answer(referent="president" if False else "presidente", president="Zapatero", addressee="")
    )
    c = resolve(claim("con el señor Zapatero el paro superó los cuatro millones", "SPEAKER_2"), llm)
    assert scope(c) == [(D(2004, 4, 17), D(2011, 12, 21))]
    assert "Zapatero" in c.resolved_actor and "PSOE" in c.resolved_actor


def test_nosotros_of_the_governing_party_about_the_current_term():
    llm = FakeLLM(answer(referent="orador", addressee="", current_term_only=True))
    c = resolve(claim("nosotros subimos el salario mínimo a 900 euros"), llm)
    assert scope(c) == [(D(2018, 6, 2), DATE)]


def test_nosotros_without_clues_covers_every_period_of_the_party():
    llm = FakeLLM(answer(referent="orador", addressee=""))
    c = resolve(claim("nosotros subimos el salario mínimo"), llm)
    assert scope(c) == [
        (D(1982, 12, 2), D(1996, 5, 5)),
        (D(2004, 4, 17), D(2011, 12, 21)),
        (D(2018, 6, 2), DATE),
    ]


def test_current_term_flag_is_ignored_for_a_party_not_in_government():
    llm = FakeLLM(answer(referent="orador", addressee="", current_term_only=True))
    c = resolve(claim("nosotros bajamos los impuestos", "SPEAKER_2"), llm)
    assert len(c.time_scope) == 2  # both PP periods
    assert "no gobierna" in c.resolution_notes


def test_este_gobierno():
    llm = FakeLLM(answer(referent="gobierno_actual", addressee=""))
    c = resolve(claim("este Gobierno ha subido los impuestos", "SPEAKER_2"), llm)
    assert scope(c) == [(D(2018, 6, 2), DATE)]
    assert c.resolved_actor == "Gobierno de Pedro Sánchez (PSOE, en coalición con Unidas Podemos)"


def test_gobierno_anterior():
    llm = FakeLLM(answer(referent="gobierno_anterior", addressee=""))
    c = resolve(claim("el Gobierno anterior recortó la sanidad"), llm)
    assert scope(c) == [(D(2011, 12, 21), D(2018, 6, 2))]
    assert "Rajoy" in c.resolved_actor


def test_hace_diez_anos():
    llm = FakeLLM(answer(referent="momento", addressee="", years_ago=10))
    c = resolve(claim("hace diez años el paro era del 26 %"), llm)
    assert scope(c) == [(D(2013, 1, 1), D(2013, 12, 31))]
    assert "Rajoy" in c.resolved_actor


def test_years_in_the_sentence_narrow_the_party_periods():
    llm = FakeLLM(answer(years=[2013]))
    c = resolve(claim("cuando ustedes gobernaban, en 2013, el paro superó el 26 %"), llm)
    assert scope(c) == [(D(2013, 1, 1), D(2013, 12, 31))]


def test_years_outside_the_party_periods_keep_all_periods_with_a_note():
    llm = FakeLLM(answer(years=[1990]))
    c = resolve(claim("cuando ustedes gobernaban, en 1990, el paro superó el 26 %"), llm)
    assert len(c.time_scope) == 2
    assert "1990" in c.resolution_notes


def test_audience_is_not_resolved_to_any_party():
    llm = FakeLLM(answer(referent="audiencia", addressee="", notes="Se dirige a la ciudadanía."))
    c = resolve(claim("ustedes, los ciudadanos, merecen saber la verdad"), llm)
    assert c.context_status == "no_resuelto"
    assert c.resolved_actor is None and c.time_scope == []
    assert "audiencia" in c.resolution_notes


def test_unknown_speaker_cannot_resolve_ustedes():
    llm = FakeLLM(answer(addressee=""))
    c = resolve(claim("cuando ustedes gobernaban el paro superó el 26 %", "SPEAKER_3"), llm)
    assert c.context_status == "no_resuelto"
    assert "interlocutor" in c.resolution_notes


def test_addressee_without_known_party_cannot_be_resolved():
    llm = FakeLLM(answer(addressee="SPEAKER_3"))
    c = resolve(claim("cuando ustedes gobernaban el paro superó el 26 %"), llm)
    assert c.context_status == "no_resuelto"
    assert "partido" in c.resolution_notes


def test_low_confidence_is_not_resolved():
    llm = FakeLLM(answer(confidence=0.4))
    c = resolve(claim("cuando ustedes gobernaban el paro superó el 26 %"), llm)
    assert c.context_status == "no_resuelto"


def test_party_that_never_governed_is_not_resolved():
    llm = FakeLLM(answer(referent="partido", addressee="", party="Partido Inexistente"))
    c = resolve(claim("cuando gobernaba ese partido el paro bajó"), llm)
    assert c.context_status == "no_resuelto"


def test_model_says_there_is_nothing_to_resolve():
    llm = FakeLLM(answer(referent="ninguno", addressee=""))
    c = resolve(claim("nosotros, como país, tenemos 47 millones de habitantes"), llm)
    assert c.context_status == "no_necesario"
    assert c.time_scope == []


def test_claims_without_context_need_no_model_call():
    llm = FakeLLM()
    c = resolve(claim("la deuda supera el 100 % del PIB", needs_context=False), llm)
    assert c.context_status == "no_necesario" and llm.calls == []


def test_rewrite_without_placeholders_gets_the_context_appended():
    llm = FakeLLM(answer(rewritten="La tasa de paro superó el 26 %."))
    c = resolve(claim("cuando ustedes gobernaban el paro superó el 26 %"), llm)
    assert c.text_normalized.startswith("La tasa de paro superó el 26 %.")
    assert "PP" in c.text_normalized and "2011" in c.text_normalized


def test_model_errors_leave_the_claim_unresolved_but_config_errors_stop():
    c = resolve(claim("cuando ustedes gobernaban el paro superó el 26 %"), FakeLLM(LLMError("x")))
    assert c.context_status == "no_resuelto"
    with pytest.raises(LLMConfigError):
        resolve(claim("cuando ustedes gobernaban"), FakeLLM(LLMConfigError("clave")))


def test_actor_lists_only_the_presidents_of_those_periods():
    llm = FakeLLM(answer(referent="orador", addressee=""))
    c = resolve(claim("nosotros subimos el salario mínimo"), llm)  # PSOE, all periods
    assert c.resolved_actor == (
        "PSOE (gobiernos de Felipe González y José Luis Rodríguez Zapatero y Pedro Sánchez)"
    )
