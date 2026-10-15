"""`conocimiento revisar`: lo pendiente se muestra con su fuente y solo una persona lo aprueba."""

import psycopg
import pytest

from pipeline.knowledge.review import ReviewError, decide, pending_items

from .test_knowledge_schema import _observation, db  # noqa: F401  (fixture)

pytestmark = pytest.mark.supabase


def test_pending_observations_show_source_motives_and_previous_values(db):  # noqa: F811
    first = _observation(db)
    second = _observation(
        db,
        periodo="2019",
        periodo_inicio="2019-01-01",
        periodo_fin="2019-12-31",
        valor=900,
        estado="cuarentena",
        raw='{"motivos": ["salto del 22.3 % respecto al periodo anterior (máx. 20 %)"]}',
    )
    items = {i.id: i for i in pending_items(db)}
    assert {str(first), str(second)} <= set(items)
    card = "\n".join(items[str(second)].lines)
    assert "900" in card and "euros/mes" in card
    assert "https://www.boe.es/x" in card and "RD 1077/2017" in card
    assert "salto del 22.3 %" in card
    assert "periodo anterior (2018): 735.9" in card
    assert items[str(second)].group == "observacion:smi"
    assert [i.id for i in pending_items(db, indicator="otro")] == []


def test_decisions_are_recorded_with_the_reviewer_and_cannot_be_repeated(db):  # noqa: F811
    obs = _observation(db)
    decide(db, "observacion", str(obs), "aprobado", reviewer="Jorge")
    assert db.execute("select estado from ref.observations where id = %s", (obs,)).fetchone() == (
        "aprobado",
    )
    event = db.execute(
        "select campo, antes, despues, revisor from ref.observation_events"
        " where observation_id = %s",
        (obs,),
    ).fetchone()
    assert event == ("estado", "pendiente", "aprobado", "Jorge")
    with pytest.raises(ReviewError, match="ya no está pendiente"):
        decide(db, "observacion", str(obs), "rechazado", reviewer="Jorge")
    assert str(obs) not in {i.id for i in pending_items(db)}


def test_a_reviewer_name_is_required(db):  # noqa: F811
    obs = _observation(db)
    with pytest.raises(ReviewError, match="revisor"):
        decide(db, "observacion", str(obs), "aprobado", reviewer="  ")


def test_governments_and_parties_are_reviewed_too(db):  # noqa: F811
    from pipeline.knowledge.governments_load import load_governments

    load_governments(db)
    items = pending_items(db, kinds={"gobierno", "partido"})
    gov = next(i for i in items if i.kind == "gobierno" and "Sánchez" in i.title)
    assert any("Sumar" in line or "sumar" in line for line in gov.lines)
    decide(db, "gobierno", gov.id, "aprobado", reviewer="Jorge")
    row = db.execute(
        "select estado, revisado_por, revisado_en is not null from ref.governments where id = %s",
        (gov.id,),
    ).fetchone()
    assert row == ("aprobado", "Jorge", True)
    party = next(i for i in items if i.kind == "partido")
    decide(db, "partido", party.id, "rechazado", reviewer="Jorge")
    assert party.id not in {i.id for i in pending_items(db, kinds={"partido"})}


def test_unknown_items_are_an_error(db):  # noqa: F811
    with pytest.raises(ReviewError):
        decide(db, "gobierno", "no-existe", "aprobado", reviewer="Jorge")
    with pytest.raises((ReviewError, psycopg.Error)):
        decide(db, "observacion", "abc", "aprobado", reviewer="Jorge")


def test_pending_official_data_is_approved_automatically_but_quarantine_is_not(db):  # noqa: F811
    from pipeline.knowledge.review import OFFICIAL_REVIEWER, approve_official

    pending = _observation(db)
    quarantined = _observation(
        db, periodo="2019", periodo_inicio="2019-01-01", periodo_fin="2019-12-31",
        estado="cuarentena",
    )  # fmt: skip
    assert approve_official(db) == {"smi": 1}
    states = dict(db.execute("select id, estado from ref.observations").fetchall())
    assert (states[pending], states[quarantined]) == ("aprobado", "cuarentena")
    revisor = db.execute(
        "select revisor from ref.observation_events where observation_id = %s", (pending,)
    ).fetchone()
    assert revisor == (OFFICIAL_REVIEWER,)
