"""Políticos en Supabase (proyecto de desarrollo, dentro de una transacción deshecha)."""

import datetime as dt
import json
from pathlib import Path

import psycopg
import pytest

from pipeline.knowledge.politicians_repo import load_politicians, read_politicians, record_research
from pipeline.knowledge.review import decide, pending_items
from pipeline.politicians import Membership, Politician, PoliticianSource, Role

from .test_knowledge_schema import _fails, db  # noqa: F401  (fixture)

pytestmark = pytest.mark.supabase
D = dt.date
BOE = PoliticianSource(organismo="BOE", titulo="RD 1/2024", url="https://www.boe.es/a")
WIKI = PoliticianSource(
    organismo="Wikipedia", titulo="Ana Gil", url="https://es.wikipedia.org/a", tipo="secundaria"
)
ANA = Politician(
    id="ana-gil-ruiz",
    nombre="Ana Gil Ruiz",
    alias=["Gil Ruiz"],
    fecha_nacimiento=D(1970, 5, 2),
    fecha_nacimiento_source=WIKI,
    origen="curado",
    estado="pendiente",
    parties=[Membership(partido="Partido Popular", inicio=D(2015, 7, 1), source=BOE)],
    roles=[Role(cargo="Diputada", organo="Congreso", inicio=D(2023, 8, 17), source=BOE)],
)


def _json(tmp_path: Path, *people: Politician) -> Path:
    path = tmp_path / "politicians.json"
    path.write_text(
        json.dumps({"politicos": [p.model_dump(mode="json") for p in people]}), encoding="utf-8"
    )
    return path


def test_curated_politicians_enter_pending_and_are_used_only_once_approved(db, tmp_path):  # noqa: F811
    report = load_politicians(db, _json(tmp_path, ANA))
    assert report.new == ["ana-gil-ruiz"]
    assert read_politicians(db) == []  # pending: not usable
    [item] = [i for i in pending_items(db, kinds={"politico"})]
    card = "\n".join(item.lines)
    assert "1970-05-02" in card and "secundaria" in card and "Partido Popular" in card
    decide(db, "politico", "ana-gil-ruiz", "aprobado", reviewer="Jorge")
    [ana] = read_politicians(db)
    assert ana.model_dump(exclude={"estado"}) == ANA.model_dump(exclude={"estado"})
    assert load_politicians(db, _json(tmp_path, ANA)).unchanged == ["ana-gil-ruiz"]


def test_changed_curated_entries_go_back_to_review_only_when_asked(db, tmp_path):  # noqa: F811
    load_politicians(db, _json(tmp_path, ANA))
    decide(db, "politico", "ana-gil-ruiz", "aprobado", reviewer="Jorge")
    changed = ANA.model_copy(update={"alias": ["Gil Ruiz", "Anita"]})
    assert load_politicians(db, _json(tmp_path, changed)).changed == ["ana-gil-ruiz"]
    assert load_politicians(db, _json(tmp_path, changed), update=True).updated == ["ana-gil-ruiz"]
    assert read_politicians(db) == []  # back to pending


def test_research_results_are_used_at_once_and_grow_until_reviewed(db):  # noqa: F811
    seen = Membership(partido="PP", constatado_en=D(2023, 9, 1), source=BOE)
    auto = ANA.model_copy(
        update={
            "origen": "investigacion",
            "estado": "automatico",
            "parties": [seen],
            "roles": [],
            "fecha_nacimiento": None,
            "fecha_nacimiento_source": None,
        }
    )
    assert record_research(db, auto) == "nuevo"
    later = auto.model_copy(
        update={
            "alias": ["Sra. Gil"],
            "parties": [seen.model_copy(update={"constatado_en": D(2024, 3, 1)})],
        }
    )
    assert record_research(db, later) == "actualizado"
    [ana] = read_politicians(db)
    assert ana.estado == "automatico" and len(ana.parties) == 2 and "Sra. Gil" in ana.alias
    decide(db, "politico", "ana-gil-ruiz", "rechazado", reviewer="Jorge")
    assert read_politicians(db) == []
    assert record_research(db, later) == "sin_cambios"  # a rejected entry is not reopened


def test_curated_data_can_never_be_automatic(db):  # noqa: F811
    _fails(
        db,
        "insert into ref.politicians (id, nombre, origen, estado)"
        " values ('x-y', 'X Y', 'curado', 'automatico')",
        match="politicians_automatico_chk",
    )
    _fails(
        db,
        "insert into ref.politicians (id, nombre, origen, estado, fecha_nacimiento)"
        " values ('x-y', 'X Y', 'curado', 'pendiente', '1970-01-01')",
        match="politicians_nacimiento_fuente_chk",
    )


def test_duplicated_ids_in_the_json_are_rejected(db, tmp_path):  # noqa: F811
    with pytest.raises(ValueError, match="duplicados"):
        load_politicians(db, _json(tmp_path, ANA, ANA))
    assert isinstance(db, psycopg.Connection)
