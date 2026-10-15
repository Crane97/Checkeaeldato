"""La base de conocimiento vista como base local de indicadores, resuelta a una fecha."""

import datetime as dt

import psycopg
import pytest

from pipeline.evidence.local import local_evidence
from pipeline.knowledge.local_base import CatalogEntry, KnowledgeIndicatorBase
from pipeline.knowledge.repository import ObservationRow, SourceRow
from pipeline.models import DetectedClaim

D = dt.date
INE = SourceRow(
    organismo="INE", titulo="EPA, serie EPA452434", url="https://www.ine.es/x", tipo="primaria"
)
ENTRY = CatalogEntry(
    "paro_epa", "Tasa de paro (EPA)", "Porcentaje de parados sobre la población activa (EPA).",
    "% de la población activa", "trimestral", "Dato trimestral sin desestacionalizar.",
    ("paro", "desempleo"), "ine",
)  # fmt: skip


def row(oid, periodo, start, end, valor, published, version=1, estimated=False):
    return ObservationRow(oid, "paro_epa", periodo, start, end, "ES", valor, ENTRY.unidad,
                          published, estimated, version, True, "aprobado", INE)  # fmt: skip


ROWS = [
    row(1, "2013T1", D(2013, 1, 1), D(2013, 3, 31), 27.16, D(2013, 4, 25)),
    row(2, "2013T1", D(2013, 1, 1), D(2013, 3, 31), 26.94, D(2014, 1, 23), version=2),
    row(3, "2013T2", D(2013, 4, 1), D(2013, 6, 30), 26.06, D(2013, 7, 28), estimated=True),
]
BASE = KnowledgeIndicatorBase({"paro_epa": ENTRY}, {"paro_epa": ROWS})


def test_values_are_those_published_on_the_speech_date_with_later_revisions_noted():
    on_may_2013 = BASE.at(D(2013, 5, 10)).evidence("paro_epa", D(2013, 1, 1), D(2013, 3, 31))
    assert "2013T1 27,16 (revisado posteriormente a 26,94)" in on_may_2013.excerpt
    assert on_may_2013.observation_ids == [1]
    later = BASE.at(D(2014, 6, 1)).evidence("paro_epa", D(2013, 1, 1), D(2013, 6, 30))
    assert "2013T1 26,94" in later.excerpt and "revisado" not in later.excerpt.split("Notas")[0]
    assert later.observation_ids == [2, 3]
    assert "estimada por el calendario" in later.excerpt
    assert BASE.at(D(2013, 4, 1)).evidence("paro_epa", D(2013, 1, 1), D(2013, 3, 31)) is None


def test_claims_about_now_use_the_latest_value_published_by_then():
    claim = DetectedClaim(
        id="c1", speaker="A", text_original="el paro está en el 26 %",
        text_normalized="La tasa de paro EPA es del 26 %", claim_type="estadistica",
        indicator="paro_epa", years=[2013],
    )  # fmt: skip
    item = local_evidence(claim, BASE.at(D(2013, 8, 1)), D(2013, 8, 1))
    assert item is not None and "2013T2 26,06" in item.excerpt


def test_catalog_lists_synonyms_and_version_follows_the_data():
    assert "Se nombra como: paro, desempleo" in BASE.catalog()
    more = KnowledgeIndicatorBase(
        {"paro_epa": ENTRY},
        {
            "paro_epa": [
                *ROWS,
                row(4, "2013T3", D(2013, 7, 1), D(2013, 9, 30), 25.98, D(2013, 10, 24)),
            ]
        },
    )
    assert more.version(coverage=False) == BASE.version(coverage=False)
    assert more.version(coverage=True) != BASE.version(coverage=True)


@pytest.mark.supabase
def test_the_development_base_answers_a_golden_value(supabase_url):
    with psycopg.connect(supabase_url, connect_timeout=20) as conn:
        conn.read_only = True
        base = KnowledgeIndicatorBase.from_db(conn)
        conn.rollback()
    if "paro_epa" not in base.rows:
        pytest.skip("La base de desarrollo no tiene paro_epa cargado")
    item = base.at(D(2026, 10, 14)).evidence("paro_epa", D(2013, 1, 1), D(2013, 3, 31))
    assert "2013T1 26,94" in item.excerpt and item.observation_ids
    assert item.source.kind == "indicador_local"


def _territorial_base() -> KnowledgeIndicatorBase:
    andalucia = [
        ObservationRow(10 + i, "paro_epa", p, s, e, "ES-AN", v, ENTRY.unidad, pub, False, 1,
                       True, "aprobado", INE)  # fmt: skip
        for i, (p, s, e, v, pub) in enumerate([
            ("2012T4", D(2012, 10, 1), D(2012, 12, 31), 35.71, D(2013, 1, 28)),
            ("2013T1", D(2013, 1, 1), D(2013, 3, 31), 36.77, D(2013, 4, 28)),
        ])
    ]  # fmt: skip
    return KnowledgeIndicatorBase(
        {"paro_epa": ENTRY},
        {"paro_epa": ROWS, "paro_epa@ES-AN": andalucia},
        territory_names={"ES-AN": "Andalucía"},
    )


def test_regional_claims_use_the_regional_series_and_never_the_national_one():
    base = _territorial_base()
    regional = DetectedClaim(
        id="c1", speaker="A", text_original="en Andalucía el paro superó el 36 %",
        text_normalized="La tasa de paro EPA de Andalucía superó el 36 % en 2013",
        claim_type="estadistica", indicator="paro_epa", years=[2013],
        territory="Andalucía", territory_code="ES-AN",
    )  # fmt: skip
    item = local_evidence(regional, base.at(D(2014, 1, 1)), D(2014, 1, 1))
    assert "Andalucía" in item.excerpt and "36,77" in item.excerpt and "27,16" not in item.excerpt
    elsewhere = regional.model_copy(update={"territory": "Murcia", "territory_code": "ES-MC"})
    assert local_evidence(elsewhere, base.at(D(2014, 1, 1)), D(2014, 1, 1)) is None
    assert "por comunidad autónoma" in base.catalog()


def test_detection_drops_the_indicator_when_the_territory_is_ambiguous():
    from pipeline.claims import ExtractedClaim, _indicator_for, _territory_code

    def item(territory):
        return ExtractedClaim(
            segment=0, quote="x", text_normalized="x", claim_type="estadistica",
            search_queries=[], speaker="", needs_context=False, indicator="paro_epa",
            territory=territory,
        )  # fmt: skip

    clear = item("Andalucía")
    assert (_indicator_for(clear, {"paro_epa"}), _territory_code(clear)) == ("paro_epa", "ES-AN")
    assert _indicator_for(item("Madrid"), {"paro_epa"}) is None  # region or province?
    assert (_indicator_for(item(""), {"paro_epa"}), _territory_code(item(""))) == ("paro_epa", None)
