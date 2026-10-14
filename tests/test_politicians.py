"""Base de políticos: emparejar nombres, datos en una fecha y reutilización en speaker_research."""

import datetime as dt
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.governments import default_governments
from pipeline.politicians import (
    CombinedDirectory,
    LocalPoliticianCache,
    Membership,
    Politician,
    PoliticianSource,
    Role,
    match,
    slug,
)
from pipeline.speaker_research import research_speakers

from .test_speaker_research import GIL, GOOD, OFFICIAL, WEB, FakeLLM, FakeSearch, speakers

D = dt.date
BOE = PoliticianSource(organismo="BOE", titulo="RD 1/2024", url="https://www.boe.es/a")
WIKI = PoliticianSource(
    organismo="Wikipedia", titulo="Ana Gil", url="https://es.wikipedia.org/a", tipo="secundaria"
)


def person(pid: str, nombre: str, estado="aprobado", origen="curado", **kw) -> Politician:
    return Politician(id=pid, nombre=nombre, estado=estado, origen=origen, **kw)


ANA = person(
    "ana-gil-ruiz",
    "Ana Gil Ruiz",
    alias=["Anita Gil"],
    fecha_nacimiento=D(1970, 5, 2),
    fecha_nacimiento_source=WIKI,
    parties=[
        Membership(
            partido="Partido Socialista Obrero Español",
            inicio=D(1995, 1, 1),
            fin=D(2015, 6, 30),
            source=BOE,
        ),
        Membership(partido="Partido Popular", inicio=D(2015, 7, 1), source=BOE),
    ],
    roles=[
        Role(
            cargo="Ministra de Hacienda",
            organo="Gobierno de España",
            inicio=D(2022, 1, 1),
            fin=D(2023, 7, 23),
            source=BOE,
        ),
        Role(
            cargo="Ministra de Hacienda",
            organo="Gobierno de España",
            inicio=D(2023, 7, 24),
            fin=D(2023, 11, 20),
            en_funciones=True,
            source=BOE,
        ),
    ],
)
PEDRO_GIL = person(
    "pedro-gil", "Pedro Gil", parties=[Membership(partido="X", inicio=D(2000, 1, 1), source=BOE)]
)


def test_names_match_by_all_their_words_and_ambiguity_gives_nothing():
    people = [ANA, PEDRO_GIL]
    assert match("Sra. Gil Ruiz", people) is ANA
    assert match("señora gil ruíz", people) is ANA
    assert match("Anita Gil", people) is ANA
    assert match("Gil", people) is None  # two people called Gil
    assert match("Luis Pérez", people) is None


def test_pending_and_rejected_people_are_never_matched():
    assert match("Gil Ruiz", [ANA.model_copy(update={"estado": "pendiente"})]) is None
    assert match("Gil Ruiz", [ANA.model_copy(update={"estado": "rechazado"})]) is None


def test_party_and_roles_on_a_date():
    assert ANA.party_on(D(2010, 1, 1)).partido.startswith("Partido Socialista")
    assert ANA.party_on(D(2020, 1, 1)).partido == "Partido Popular"
    [role] = ANA.roles_on(D(2023, 9, 1))
    assert role.en_funciones
    assert ANA.roles_on(D(2024, 1, 1)) == []


def test_snapshots_from_research_are_valid_for_a_year_only():
    seen = Membership(partido="Partido Popular", constatado_en=D(2023, 2, 21), source=BOE)
    p = person(
        "ana-gil-ruiz", "Ana Gil Ruiz", estado="automatico", origen="investigacion", parties=[seen]
    )
    assert p.party_on(D(2023, 12, 1)).partido == "Partido Popular"
    assert p.party_on(D(2025, 6, 1)) is None


def test_conflicting_parties_give_no_party():
    a = Membership(partido="A", inicio=D(2020, 1, 1), source=BOE)
    b = Membership(partido="B", constatado_en=D(2021, 1, 1), source=BOE)
    assert person("x", "X Y", parties=[a, b]).party_on(D(2021, 1, 1)) is None


def test_invalid_entries_are_rejected():
    with pytest.raises(ValidationError):  # curated data is never automatic
        person("x", "X", estado="automatico")
    with pytest.raises(ValidationError):  # a birth date needs a source
        person("x", "X", fecha_nacimiento=D(1970, 1, 1))
    with pytest.raises(ValidationError):  # a role needs a date
        Role(cargo="Diputada", source=BOE)
    assert slug("Sr. Alberto Núñez Feijóo") == "alberto-nunez-feijoo"


def test_local_cache_records_merges_and_forgets(tmp_path: Path):
    cache = LocalPoliticianCache(tmp_path / "p.json")
    auto = person(
        "ana-gil-ruiz",
        "Ana Gil Ruiz",
        estado="automatico",
        origen="investigacion",
        parties=[Membership(partido="PP", constatado_en=D(2023, 1, 1), source=BOE)],
    )
    cache.record(auto)
    later = auto.model_copy(
        update={
            "alias": ["Gil"],
            "parties": [Membership(partido="PP", constatado_en=D(2024, 1, 1), source=BOE)],
        }
    )
    cache.record(later)
    [stored] = cache.usable()
    assert stored.alias == ["Gil"] and len(stored.parties) == 2
    cache.forget({"ana-gil-ruiz"})
    assert cache.usable() == []
    assert json.loads((tmp_path / "p.json").read_text()) == {}


class FakeDB:
    def __init__(self, people=(), rejected=(), fail=False):
        self.people, self.rejected, self.fail = list(people), set(rejected), fail
        self.recorded: list[Politician] = []

    def usable(self):
        if self.fail:
            raise OSError("sin red")
        return self.people

    def rejected_ids(self):
        return self.rejected

    def record(self, p):
        self.recorded.append(p)


def run(smap, directory, search=None, llm=None):
    return research_speakers(
        smap,
        speech_date=D(2023, 9, 1),
        search=search,
        llm=llm or FakeLLM(),
        model="m",
        governments=default_governments(),
        directory=directory,
    )


def test_a_known_politician_needs_no_search(tmp_path: Path):
    search = FakeSearch([OFFICIAL], [WEB])
    directory = CombinedDirectory(LocalPoliticianCache(tmp_path / "p.json"), FakeDB([ANA]))
    [gil] = run(speakers(GIL), directory, search).speakers
    assert search.calls == []
    assert (gil.name, gil.party) == ("Ana Gil Ruiz", "PP")
    assert gil.role == "líder de la oposición"  # what the debate said is kept
    assert gil.sources[-1] == "base_politicos"
    assert [r.url for r in gil.references] == ["https://www.boe.es/a"]


def test_the_base_works_without_search_too(tmp_path: Path):
    directory = CombinedDirectory(LocalPoliticianCache(tmp_path / "p.json"), FakeDB([ANA]))
    no_role = GIL.model_copy(update={"role": None})
    [gil] = run(speakers(no_role), directory, search=None).speakers
    assert gil.party == "PP" and gil.role == "Ministra de Hacienda (en funciones)"


def test_new_speakers_are_researched_and_recorded_as_automatic(tmp_path: Path):
    db = FakeDB()
    cache = LocalPoliticianCache(tmp_path / "p.json")
    [gil] = run(
        speakers(GIL), CombinedDirectory(cache, db), FakeSearch([OFFICIAL], [WEB]), FakeLLM(GOOD)
    ).speakers
    assert gil.party == "PP"
    [entry] = db.recorded
    assert (entry.id, entry.estado, entry.origen) == ("ana-gil-ruiz", "automatico", "investigacion")
    assert entry.alias == ["Sra. Gil Ruiz"]
    assert entry.parties[0].constatado_en == D(2023, 9, 1)
    assert entry.parties[0].source.url == OFFICIAL.source.url
    # The next analysis finds it in the cache without searching.
    search = FakeSearch([OFFICIAL], [WEB])
    [again] = run(speakers(GIL), CombinedDirectory(cache, None), search).speakers
    assert search.calls == [] and again.party == "PP"


def test_rejected_people_are_neither_used_nor_recorded_again(tmp_path: Path):
    cache = LocalPoliticianCache(tmp_path / "p.json")
    auto = person(
        "ana-gil-ruiz",
        "Ana Gil Ruiz",
        estado="automatico",
        origen="investigacion",
        parties=[Membership(partido="Vox", constatado_en=D(2023, 9, 1), source=BOE)],
    )
    cache.record(auto)
    db = FakeDB(rejected={"ana-gil-ruiz"})
    search = FakeSearch([OFFICIAL], [WEB])
    [gil] = run(speakers(GIL), CombinedDirectory(cache, db), search, FakeLLM(GOOD)).speakers
    assert search.calls and gil.party == "PP"  # researched again, the rejected entry ignored
    assert db.recorded == [] and cache.usable() == []


def test_a_database_failure_is_reported_and_the_cache_still_works(tmp_path: Path):
    cache = LocalPoliticianCache(tmp_path / "p.json")
    cache.record(
        person(
            "ana-gil-ruiz",
            "Ana Gil Ruiz",
            estado="automatico",
            origen="investigacion",
            parties=[Membership(partido="PP", constatado_en=D(2023, 9, 1), source=BOE)],
        )
    )
    result = run(speakers(GIL), CombinedDirectory(cache, FakeDB(fail=True)))
    assert result.speakers[0].party == "PP"
    assert any("Base de políticos no disponible" in e for e in result.errors)


def test_a_dated_row_has_either_a_period_or_a_sighting():
    with pytest.raises(ValidationError):
        Role(cargo="Diputada", inicio=D(2020, 1, 1), constatado_en=D(2021, 1, 1), source=BOE)
    with pytest.raises(ValidationError):
        Membership(partido="PP", constatado_en=D(2021, 1, 1), fin=D(2022, 1, 1), source=BOE)
