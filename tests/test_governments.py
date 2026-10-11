import datetime as dt
import json
from pathlib import Path

import pytest

from pipeline.governments import (
    DEFAULT_KNOWLEDGE_PATH,
    DateRange,
    Governments,
    KnowledgeError,
    load_knowledge,
)

D = dt.date
SPEECH_2023 = D(2023, 7, 10)


@pytest.fixture(scope="module")
def gov() -> Governments:
    return Governments(load_knowledge())


def ranges(periods: list[DateRange]) -> list[tuple[dt.date, dt.date]]:
    return [(p.start, p.end) for p in periods]


def test_bundled_table_is_valid_and_versioned():
    knowledge = load_knowledge()
    assert DEFAULT_KNOWLEDGE_PATH.parts[-3:] == ("data", "knowledge", "governments.json")
    assert knowledge.version >= 1
    assert [g.president for g in knowledge.governments][-1] == "Pedro Sánchez"


@pytest.mark.parametrize(
    ("day", "president"),
    [
        (D(1977, 6, 15), "Adolfo Suárez"),
        (D(1990, 1, 1), "Felipe González"),
        (D(2013, 3, 31), "Mariano Rajoy"),
        (D(2016, 3, 1), "Mariano Rajoy"),  # en funciones
        (D(2018, 6, 2), "Pedro Sánchez"),  # handover day belongs to the incoming president
        (D(2018, 6, 1), "Mariano Rajoy"),
        (D(2023, 7, 10), "Pedro Sánchez"),
    ],
)
def test_government_at(gov, day, president):
    assert gov.at(day).president == president


def test_no_government_before_the_table(gov):
    assert gov.at(D(1970, 1, 1)) is None


def test_previous_government(gov):
    assert gov.previous(SPEECH_2023).president == "Mariano Rajoy"
    assert gov.previous(D(2010, 1, 1)).president == "José María Aznar"
    assert gov.previous(D(1977, 1, 1)) is None


def test_party_periods_merge_terms_and_stop_at_the_speech_date(gov):
    assert ranges(gov.party_periods("PP", until=SPEECH_2023)) == [
        (D(1996, 5, 5), D(2004, 4, 17)),
        (D(2011, 12, 21), D(2018, 6, 2)),
    ]
    assert ranges(gov.party_periods("PSOE", until=SPEECH_2023)) == [
        (D(1982, 12, 2), D(1996, 5, 5)),
        (D(2004, 4, 17), D(2011, 12, 21)),
        (D(2018, 6, 2), SPEECH_2023),
    ]


def test_party_periods_before_a_date_exclude_later_governments(gov):
    assert ranges(gov.party_periods("PSOE", until=D(2000, 1, 1))) == [
        (D(1982, 12, 2), D(1996, 5, 5))
    ]


def test_coalition_partners_count_as_governing(gov):
    assert ranges(gov.party_periods("Unidas Podemos", until=D(2025, 1, 1))) == [
        (D(2020, 1, 13), D(2023, 11, 21))
    ]
    assert ranges(gov.party_periods("Sumar", until=D(2025, 1, 1))) == [
        (D(2023, 11, 21), D(2025, 1, 1))
    ]
    assert gov.partners_at(D(2021, 5, 1)) == ["Unidas Podemos"]
    assert gov.partners_at(D(2024, 5, 1)) == ["Sumar"]
    assert gov.partners_at(D(2019, 5, 1)) == []


def test_party_aliases(gov):
    assert gov.canonical_party("Partido Popular") == "PP"
    assert gov.canonical_party("los socialistas") == "PSOE"
    assert gov.canonical_party("psoe") == "PSOE"
    assert gov.canonical_party("Podemos") == "Unidas Podemos"
    assert gov.canonical_party("Partido Inexistente") is None


def test_president_periods_match_by_surname(gov):
    assert ranges(gov.president_periods("Zapatero", until=SPEECH_2023)) == [
        (D(2004, 4, 17), D(2011, 12, 21))
    ]
    assert ranges(gov.president_periods("señor Rajoy", until=SPEECH_2023)) == [
        (D(2011, 12, 21), D(2018, 6, 2))
    ]
    assert gov.president_periods("Desconocido", until=SPEECH_2023) == []


def test_current_tenure_is_the_continuous_period_of_the_governing_party(gov):
    assert ranges(gov.current_party_tenure(SPEECH_2023)) == [(D(2018, 6, 2), SPEECH_2023)]


def test_describe_government(gov):
    text = gov.describe(gov.at(D(2021, 1, 1)), D(2021, 1, 1))
    assert text == "Gobierno de Pedro Sánchez (PSOE, en coalición con Unidas Podemos)"
    assert gov.describe(gov.at(D(2016, 3, 1)), D(2016, 3, 1)).endswith("(PP, en funciones)")


def test_year_window(gov):
    assert ranges([gov.year_range(2013)]) == [(D(2013, 1, 1), D(2013, 12, 31))]


def _write(tmp_path: Path, governments: list[dict]) -> Path:
    path = tmp_path / "g.json"
    path.write_text(
        json.dumps(
            {"version": 1, "updated": "2026-01-01", "parties": {}, "governments": governments}
        )
    )
    return path


def _term(gid: str, start: str, end: str | None) -> dict:
    return {
        "id": gid,
        "level": "nacional",
        "territory": "España",
        "president": gid,
        "parties": ["X"],
        "start": start,
        "end": end,
    }


def test_overlapping_terms_are_rejected(tmp_path):
    path = _write(
        tmp_path, [_term("a", "2000-01-01", "2005-01-01"), _term("b", "2004-01-01", None)]
    )
    with pytest.raises(KnowledgeError, match="solapan"):
        load_knowledge(path)


def test_only_the_last_term_may_be_open(tmp_path):
    path = _write(tmp_path, [_term("a", "2000-01-01", None), _term("b", "2004-01-01", None)])
    with pytest.raises(KnowledgeError):
        load_knowledge(path)
