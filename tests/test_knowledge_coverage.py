"""Cobertura por partido y tema (fase G)."""

import pytest

from pipeline.knowledge.catalog import load_catalog
from pipeline.knowledge.coverage import coverage, load_topics, topic_of

from .test_knowledge_schema import db  # noqa: F401  (fixture)

TOPICS = load_topics()


def test_every_catalog_topic_is_a_coverage_topic():
    assert {s.tema for s in load_catalog().values()} - {"otros"} <= set(TOPICS)


def test_topics_come_from_the_indicator_or_the_most_keywords():
    assert topic_of("El paro bajó", "empleo", TOPICS) == "empleo"
    assert topic_of("Las listas de espera en los hospitales crecieron", None, TOPICS) == "sanidad"
    assert topic_of("Subieron el IVA de la luz", None, TOPICS) == "fiscalidad"  # tie: listed first
    assert topic_of("El fiscal general del Estado fue procesado", None, TOPICS) == "instituciones"
    assert topic_of("Una afirmación sin tema reconocible", None, TOPICS) == "otros"
    assert topic_of("Hubo una huelga de gasolineras", None, TOPICS) == "otros"  # whole words
    assert topic_of("x", "tema_desconocido", TOPICS) == "otros"


def _claims(db, n, partido, tema, veredicto, con_datos=False):  # noqa: F811
    for _ in range(n):
        db.execute(
            "insert into verif.claims (job_id, claim_id, claim_normalizado, veredicto,"
            " evidencia, partido, tema, con_datos) values ('j', 'c', 'x', %s, '[]', %s, %s, %s)",
            (veredicto, partido, tema, con_datos),
        )


@pytest.mark.supabase
def test_uneven_coverage_between_parties_is_flagged_in_both_directions(db):  # noqa: F811
    _claims(db, 12, "A", "empleo", "verdadero", con_datos=True)
    _claims(db, 8, "B", "sanidad", "no_verificable")
    _claims(db, 4, "B", "sanidad", "verdadero")
    _claims(db, 3, "C", "otros", "no_verificable")
    _claims(db, 2, None, "otros", "verdadero")
    report = coverage(db)
    assert {g.name: g.total for g in report.parties}["B"] == 12
    b = [a for a in report.alerts if a.startswith("B:")]
    assert any("no verificable" in a for a in b) and any("series oficiales" in a for a in b)
    assert all("puede deberse al tipo de afirmaciones" in a for a in report.alerts)
    assert not any(a.startswith("A:") and "no verificable" in a for a in report.alerts)
    assert any(g.startswith("sanidad") for g in report.gaps)
    assert any(e.startswith("C (3)") for e in report.excluded)
    assert any(e.startswith("sin partido identificado") for e in report.excluded)
