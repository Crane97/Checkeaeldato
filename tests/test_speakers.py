import datetime as dt
from typing import Any

import pytest

from pipeline.governments import default_governments
from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import Segment, Speaker, SpeakerInfo, Transcript
from pipeline.speakers import (
    PROMPT,
    SpeakerGuess,
    SpeakerIdentification,
    format_excerpt,
    identify_speakers,
    parse_speaker_lines,
)

DATE = dt.date(2023, 7, 10)
MODEL = "claude-haiku-5-5"


def transcript(*turns: tuple[str, str]) -> Transcript:
    return Transcript(
        provider="elevenlabs",
        language="es",
        duration=600,
        segments=[
            Segment(speaker=label, text=text, start=i * 10.0, end=i * 10.0 + 9)
            for i, (label, text) in enumerate(turns)
        ],
    )


DEBATE = transcript(
    (
        "SPEAKER_0",
        "Buenas noches. Hoy debaten Ana Ruiz, candidata del Partido Socialista, "
        "y Luis Gómez, candidato del Partido Popular.",
    ),
    ("SPEAKER_1", "Gracias. Señor Gómez, cuando ustedes gobernaban el paro superó el 26 %."),
    ("SPEAKER_2", "Señora Ruiz, eso no es así."),
)


def guess(label: str, **fields: Any) -> SpeakerGuess:
    data = {"label": label, "name": "", "party": "", "role": "", "evidence": [], "confidence": 0.9}
    return SpeakerGuess(**data | fields)


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


def run(tr: Transcript, llm: FakeLLM, info: dict[str, SpeakerInfo] | None = None):
    return identify_speakers(
        tr, speech_date=DATE, llm=llm, model=MODEL, info=info, governments=default_governments()
    )


PRESENTATION = (
    "Hoy debaten Ana Ruiz, candidata del Partido Socialista, "
    "y Luis Gómez, candidato del Partido Popular"
)


def test_speakers_identified_from_the_presentation_with_literal_evidence():
    llm = FakeLLM(
        SpeakerIdentification(
            speakers=[
                guess("SPEAKER_0", role="moderadora", evidence=["Buenas noches. Hoy debaten"]),
                guess(
                    "SPEAKER_1",
                    name="Ana Ruiz",
                    party="Partido Socialista",
                    role="candidata",
                    evidence=[PRESENTATION, "Señora Ruiz, eso no es así"],
                ),
                guess(
                    "SPEAKER_2",
                    name="Luis Gómez",
                    party="Partido Popular",
                    role="candidato",
                    evidence=[PRESENTATION, "Señor Gómez, cuando ustedes gobernaban"],
                ),
            ]
        )
    )
    result = run(DEBATE, llm)

    ana = result.get("SPEAKER_1")
    assert (ana.name, ana.party, ana.role) == ("Ana Ruiz", "PSOE", "candidata")
    assert ana.sources == ["texto"]
    assert result.get("SPEAKER_2").party == "PP"
    assert result.get("SPEAKER_0").role == "moderadora"
    assert llm.calls[0]["prompt"] == PROMPT


def test_invented_evidence_is_rejected():
    llm = FakeLLM(
        SpeakerIdentification(
            speakers=[
                guess(
                    "SPEAKER_1",
                    name="Ana Ruiz",
                    party="Partido Socialista",
                    evidence=["La candidata socialista Ana Ruiz"],
                )
            ]
        )
    )
    speaker = run(DEBATE, llm).get("SPEAKER_1")
    assert (speaker.name, speaker.party) == (None, None)
    assert speaker.confidence == 0.0


def test_party_must_appear_in_the_evidence():
    llm = FakeLLM(
        SpeakerIdentification(
            speakers=[
                guess(
                    "SPEAKER_2",
                    name="Luis Gómez",
                    party="Vox",
                    evidence=["Señor Gómez, cuando ustedes gobernaban"],
                )
            ]
        )
    )
    speaker = run(DEBATE, llm).get("SPEAKER_2")
    assert speaker.name == "Luis Gómez"
    assert speaker.party is None  # never taken from what the model "knows"


def test_low_confidence_means_unknown():
    llm = FakeLLM(
        SpeakerIdentification(
            speakers=[
                guess(
                    "SPEAKER_1",
                    name="Ana Ruiz",
                    party="Partido Socialista",
                    evidence=[PRESENTATION],
                    confidence=0.4,
                )
            ]
        )
    )
    speaker = run(DEBATE, llm).get("SPEAKER_1")
    assert speaker.name is None and speaker.party is None


def test_user_info_wins_and_skips_the_model_for_that_label():
    llm = FakeLLM(
        SpeakerIdentification(
            speakers=[
                guess(
                    "SPEAKER_1",
                    name="Otra Persona",
                    party="Partido Popular",
                    evidence=[PRESENTATION],
                )
            ]
        )
    )
    info = {"SPEAKER_1": SpeakerInfo(name="Ana Ruiz", party="PSOE", role="candidata")}
    speaker = run(DEBATE, llm, info).get("SPEAKER_1")
    assert (speaker.name, speaker.party, speaker.confidence) == ("Ana Ruiz", "PSOE", 1.0)
    assert speaker.sources == ["archivo"]


def test_user_info_can_be_keyed_by_the_name_used_as_label():
    tr = transcript(("Ana Ruiz", "Hola."), ("Luis Gómez", "Adiós."))
    info = {"Luis Gómez": SpeakerInfo(party="Partido Popular")}
    result = run(tr, FakeLLM(SpeakerIdentification(speakers=[])), info)
    assert result.get("Luis Gómez").party == "PP"
    assert result.get("Ana Ruiz").name == "Ana Ruiz"  # text labels are already names


def test_presidents_get_their_party_from_the_government_table():
    tr = transcript(
        ("SPEAKER_0", "Tiene la palabra el presidente del Gobierno, Pedro Sánchez."),
        ("SPEAKER_1", "Gracias."),
    )
    llm = FakeLLM(
        SpeakerIdentification(
            speakers=[
                guess(
                    "SPEAKER_1",
                    name="Pedro Sánchez",
                    role="presidente del Gobierno",
                    evidence=["Tiene la palabra el presidente del Gobierno, Pedro Sánchez"],
                )
            ]
        )
    )
    speaker = run(tr, llm).get("SPEAKER_1")
    assert speaker.party == "PSOE"
    assert "tabla_gobiernos" in speaker.sources


def test_unknown_labels_from_the_model_are_ignored_and_every_label_is_listed():
    llm = FakeLLM(
        SpeakerIdentification(speakers=[guess("SPEAKER_9", name="X", evidence=["Gracias"])])
    )
    result = run(DEBATE, llm)
    assert [s.label for s in result.speakers] == ["SPEAKER_0", "SPEAKER_1", "SPEAKER_2"]
    assert all(s.name is None for s in result.speakers)


def test_model_failure_leaves_speakers_unknown_but_config_errors_stop():
    result = run(DEBATE, FakeLLM(LLMError("timeout")))
    assert all(s.confidence == 0 for s in result.speakers)
    with pytest.raises(LLMConfigError):
        run(DEBATE, FakeLLM(LLMConfigError("clave")))


def test_everything_given_by_the_user_needs_no_model_call():
    info = {
        label: SpeakerInfo(name=f"N{i}", party="PP")
        for i, label in enumerate(["SPEAKER_0", "SPEAKER_1", "SPEAKER_2"])
    }
    llm = FakeLLM()
    run(DEBATE, llm, info)
    assert llm.calls == []


def test_excerpt_prioritises_presentations_and_vocatives():
    filler = [("SPEAKER_1", f"Texto sin interés número {i}.") for i in range(400)]
    tr = transcript(*filler, ("SPEAKER_2", "Señora presidenta, le respondo."))
    excerpt = format_excerpt(tr, max_chars=2000)
    assert "Señora presidenta" in excerpt
    assert len(excerpt) <= 2400


def test_web_speaker_lines():
    info = parse_speaker_lines("Ana Ruiz | PSOE | candidata\nSPEAKER_2 | Partido Popular\n\n| x")
    assert info["Ana Ruiz"] == SpeakerInfo(name="Ana Ruiz", party="PSOE", role="candidata")
    assert info["SPEAKER_2"] == SpeakerInfo(name=None, party="Partido Popular", role=None)
    assert len(info) == 2


def test_president_found_in_the_table_gets_the_full_name():
    from pipeline.speakers import _with_table

    partial = Speaker(label="Presidente", name="Sánchez", role="presidente del Gobierno")
    found = _with_table(partial, dt.date(2023, 2, 21), default_governments())
    assert (found.name, found.party) == ("Pedro Sánchez", "PSOE")


def test_a_namesake_is_not_renamed_after_the_president():
    from pipeline.speakers import _with_table

    other = Speaker(label="Ana Sánchez", name="Ana Sánchez", party="PP")
    assert _with_table(other, dt.date(2023, 2, 21), default_governments()).name == "Ana Sánchez"
