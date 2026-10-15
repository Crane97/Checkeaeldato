"""Territorios (fase H): nombres del INE, alias y resolución sin ambigüedad."""

import pytest

from pipeline.knowledge.territories import (
    Territory,
    load_territories,
    resolve,
    sync_territories,
    usual_names,
)

from .test_knowledge_schema import db  # noqa: F401  (fixture)

TERRITORIES = load_territories()


def test_the_bundled_file_has_every_region_and_province():
    levels = [t.nivel for t in TERRITORIES]
    assert (levels.count("ccaa"), levels.count("provincia")) == (19, 52)
    provinces = [t for t in TERRITORIES if t.nivel == "provincia"]
    assert all(p.parent_code and p.parent_code.startswith("ES-") for p in provinces)


def test_ine_names_get_their_usual_forms():
    assert usual_names("Madrid, Comunidad de") == ["Comunidad de Madrid", "Madrid"]
    assert usual_names("Alicante/Alacant") == ["Alicante", "Alacant"]
    assert usual_names("Rioja, La") == ["La Rioja", "Rioja"]


def test_ambiguous_names_need_a_level():
    assert resolve("Comunidad de Madrid", TERRITORIES) == "ES-MD"
    assert resolve("Madrid", TERRITORIES) is None
    assert resolve("Madrid", TERRITORIES, "provincia") == "ES-P28"
    assert resolve("Euskadi", TERRITORIES) == "ES-PV"
    assert resolve("Narnia", TERRITORIES) is None


@pytest.mark.supabase
def test_territories_load_with_their_hierarchy(db):  # noqa: F811
    extra = Territory(code="ES-PX", nivel="provincia", nombre="X", parent_code="ES-MD")
    assert sync_territories(db, [*TERRITORIES, extra]) == len(TERRITORIES) + 1
    row = db.execute(
        "select nombre, parent_code, codigo_ine from ref.territories where code = 'ES-P28'"
    ).fetchone()
    assert row == ("Madrid", "ES-MD", "28")


def test_ine_word_order_resolves_too():
    assert resolve("Asturias, Principado de", TERRITORIES, "ccaa") == "ES-AS"
    assert resolve("Madrid, Comunidad de", TERRITORIES, "ccaa") == "ES-MD"
    assert resolve("Balears, Illes", TERRITORIES, "ccaa") == "ES-IB"


def test_province_prefix_sets_the_level():
    assert resolve("la provincia de Madrid", TERRITORIES) == "ES-P28"
    assert resolve("provincia de Valencia", TERRITORIES) == "ES-P46"


@pytest.mark.supabase
def test_a_code_cannot_change_level(db):  # noqa: F811
    sync_territories(db, TERRITORIES)
    wrong = Territory(code="ES-P28", nivel="ccaa", nombre="Madrid", parent_code="ES")
    with pytest.raises(ValueError, match="otro nivel"):
        sync_territories(db, [wrong])


def test_provinces_resolve_in_spanish_and_in_the_co_official_language():
    pairs = [("Lleida", "Lérida"), ("Girona", "Gerona"), ("Bizkaia", "Vizcaya"),
             ("Gipuzkoa", "Guipúzcoa"), ("Ourense", "Orense"), ("A Coruña", "La Coruña"),
             ("Araba", "Álava"), ("Alacant", "Alicante")]  # fmt: skip
    for co_official, spanish in pairs:
        code = resolve(co_official, TERRITORIES, "provincia")
        assert code is not None and code == resolve(spanish, TERRITORIES, "provincia"), spanish
