import datetime as dt
from typing import Any

from pipeline.governments import default_governments
from pipeline.llm import LLMError
from pipeline.models import EvidenceItem, Source, Speaker, SpeakerMap
from pipeline.speaker_research import ResearchAnswer, research_query, research_speakers

DATE = dt.date(2023, 2, 21)


def item(url: str, excerpt: str, kind: str = "oficial") -> EvidenceItem:
    source = Source(url=url, title="Ficha", organization="Congreso", kind=kind)
    return EvidenceItem(id="", source=source, excerpt=excerpt)


OFFICIAL = item(
    "https://www.congreso.es/diputados/gil",
    "Ana Gil Ruiz. Grupo Parlamentario Popular. Presidenta del Partido Popular desde 2022.",
)
WEB = item(
    "https://example.org/gil",
    "Ana Gil Ruiz, presidenta del Partido Popular, intervino ayer.",
    kind="web",
)

GOOD = ResearchAnswer(
    full_name="Ana Gil Ruiz",
    party="Partido Popular",
    role="presidenta del Partido Popular",
    cited=["R1"],
    quotes=["Ana Gil Ruiz. Grupo Parlamentario Popular. Presidenta del Partido Popular desde 2022"],
    confidence=0.9,
    notes="La ficha del Congreso la presenta como presidenta del partido.",
)
EMPTY = ResearchAnswer(
    full_name="", party="", role="", cited=[], quotes=[], confidence=0.2, notes="No consta."
)


class FakeSearch:
    def __init__(self, official: list[EvidenceItem], web: list[EvidenceItem]) -> None:
        self.results = {True: official, False: web}
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, query: str, *, official: bool) -> tuple[list[EvidenceItem], list[str]]:
        self.calls.append((query, official))
        return self.results[official], []


class FakeLLM:
    def __init__(self, *answers: ResearchAnswer | Exception) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def structured(self, *, user_content: str, **kwargs: Any) -> ResearchAnswer:
        self.prompts.append(user_content)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def speakers(*items: Speaker) -> SpeakerMap:
    return SpeakerMap(speakers=list(items))


def research(smap: SpeakerMap, search: FakeSearch, llm: FakeLLM) -> SpeakerMap:
    return research_speakers(
        smap,
        speech_date=DATE,
        search=search,
        llm=llm,
        model="m",
        governments=default_governments(),
    )


GIL = Speaker(
    label="Sra. Gil Ruiz", name="Sra. Gil Ruiz", role="líder de la oposición", sources=["texto"]
)


def test_query_drops_honorifics_and_asks_for_party_and_role_at_the_date():
    assert research_query("Sr. Núñez Feijóo", DATE) == "Núñez Feijóo partido político cargo 2023"
    assert research_query("señora Gil", DATE) == "Gil partido político cargo 2023"


def test_official_sources_are_enough():
    search = FakeSearch([OFFICIAL], [WEB])
    llm = FakeLLM(GOOD)
    [gil] = research(speakers(GIL), search, llm).speakers
    assert search.calls == [("Gil Ruiz partido político cargo 2023", True)]
    assert gil.name == "Ana Gil Ruiz"
    assert gil.party == "PP"  # canonical name from the governments table
    assert gil.role == "líder de la oposición"  # what the debate said is kept
    assert gil.sources == ["texto", "oficial"]
    assert [r.url for r in gil.references] == [OFFICIAL.source.url]
    assert any("Presidenta del Partido Popular" in e for e in gil.evidence)
    assert "2023-02-21" in llm.prompts[0]


def test_web_is_searched_only_when_official_sources_do_not_say():
    unrelated = item("https://www.boe.es/x", "Real Decreto sobre la ley de presupuestos.")
    search = FakeSearch([unrelated], [WEB])
    web_answer = GOOD.model_copy(
        update={
            "cited": ["R2"],
            "quotes": ["Ana Gil Ruiz, presidenta del Partido Popular"],
            "confidence": 0.7,
        }
    )
    llm = FakeLLM(EMPTY, web_answer)
    [gil] = research(speakers(GIL), search, llm).speakers
    assert [official for _, official in search.calls] == [True, False]
    assert 'id="R2" tipo="web"' in llm.prompts[1]  # official results stay first
    assert gil.party == "PP"
    assert gil.sources == ["texto", "web"]


def test_with_no_official_results_the_model_is_not_asked_twice():
    search = FakeSearch([], [])
    llm = FakeLLM()
    [gil] = research(speakers(GIL), search, llm).speakers
    assert llm.prompts == []
    assert gil.party is None


def test_quotes_not_in_the_results_are_rejected():
    search = FakeSearch([OFFICIAL], [])
    invented = GOOD.model_copy(update={"quotes": ["Ana Gil Ruiz, del Partido Popular"]})
    [gil] = research(speakers(GIL), search, FakeLLM(invented)).speakers
    assert gil.party is None


def test_party_must_appear_in_the_quotes():
    search = FakeSearch([OFFICIAL], [])
    unsupported = GOOD.model_copy(update={"party": "Partido Socialista Obrero Español"})
    [gil] = research(speakers(GIL), search, FakeLLM(unsupported)).speakers
    assert gil.party is None


def test_quotes_must_mention_the_person():
    other = item("https://www.congreso.es/x", "Luis Gil Pérez. Presidente del Partido Popular.")
    answer = GOOD.model_copy(update={"quotes": ["Luis Gil Pérez. Presidente del Partido Popular"]})
    [gil] = research(speakers(GIL), FakeSearch([other], []), FakeLLM(answer)).speakers
    assert gil.party is None


def test_low_confidence_is_ignored():
    answer = GOOD.model_copy(update={"confidence": 0.4})
    [gil] = research(speakers(GIL), FakeSearch([OFFICIAL], []), FakeLLM(answer)).speakers
    assert gil.party is None


def test_known_parties_and_unnamed_speakers_are_not_researched():
    known = Speaker(label="Presidente", name="Pedro Sánchez", party="PSOE")
    unnamed = Speaker(label="SPEAKER_0", role="moderadora")
    author = Speaker(label="autor")
    search = FakeSearch([OFFICIAL], [])
    result = research(speakers(known, unnamed, author), search, FakeLLM())
    assert search.calls == []
    assert result.speakers == [known, unnamed, author]


def test_model_failure_is_recorded_and_the_speaker_kept():
    search = FakeSearch([OFFICIAL], [])
    result = research(speakers(GIL), search, FakeLLM(LLMError("límite de uso")))
    assert result.speakers == [GIL]
    assert result.errors and "límite de uso" in result.errors[0]
