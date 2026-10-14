"""Resolución con fecha de referencia (as_of): el valor que se conocía al hablar."""

import datetime as dt

import pytest

from pipeline.knowledge.repository import (
    InMemoryRepository,
    ObservationRow,
    SourceRow,
    resolve_as_of,
)

D = dt.date
INE = SourceRow(organismo="INE", titulo="EPA", url="https://www.ine.es/epa", tipo="primaria")


def row(oid: int, valor: float, publicado: str, version: int, *, vigente: bool, **kw):
    base = {
        "id": oid,
        "indicator_id": "paro_epa",
        "periodo": "2023T1",
        "periodo_inicio": D(2023, 1, 1),
        "periodo_fin": D(2023, 3, 31),
        "territorio_code": "ES",
        "valor": valor,
        "unidad": "%",
        "fecha_publicacion": D.fromisoformat(publicado),
        "fecha_publicacion_estimada": False,
        "version": version,
        "vigente": vigente,
        "estado": "aprobado",
        "source": INE,
    }
    return ObservationRow(**base | kw)


# The first estimate (13.26) was published on 2023-04-28 and revised to 13.3 on 2024-01-26.
FIRST = row(1, 13.26, "2023-04-28", 1, vigente=False)
REVISED = row(2, 13.3, "2024-01-26", 2, vigente=True)
REPO = InMemoryRepository([FIRST, REVISED])


def resolve(as_of: dt.date):
    rows = REPO.versions("paro_epa", "ES", D(2023, 1, 1), D(2023, 3, 31))
    return resolve_as_of(rows, as_of)


def test_a_claim_made_before_the_revision_uses_the_value_known_then():
    [value] = resolve(D(2023, 6, 1))
    assert (value.observation.id, value.observation.valor) == (1, 13.26)
    assert value.revised_later is not None and value.revised_later.valor == 13.3
    assert "revisado posteriormente" in value.note()


def test_a_claim_made_after_the_revision_uses_the_revised_value():
    [value] = resolve(D(2024, 3, 1))
    assert (value.observation.id, value.observation.valor) == (2, 13.3)
    assert value.revised_later is None and value.note() == ""


def test_nothing_published_yet_at_the_speech_date():
    assert resolve(D(2023, 4, 1)) == []  # the first estimate came out on 2023-04-28


def test_pending_and_quarantined_rows_are_never_used():
    pending = row(3, 99.0, "2023-05-01", 1, vigente=True, estado="pendiente", periodo="2023T2")
    quarantine = row(4, 99.0, "2023-05-01", 1, vigente=True, estado="cuarentena", periodo="2023T3")
    repo = InMemoryRepository([FIRST, REVISED, pending, quarantine])
    rows = repo.versions("paro_epa", "ES", D(2023, 1, 1), D(2023, 12, 31))
    assert {r.id for r in rows} == {1, 2}


def test_one_value_per_period_in_period_order():
    q2 = row(
        5,
        11.6,
        "2023-07-28",
        1,
        vigente=True,
        periodo="2023T2",
        periodo_inicio=D(2023, 4, 1),
        periodo_fin=D(2023, 6, 30),
    )
    repo = InMemoryRepository([REVISED, FIRST, q2])
    values = resolve_as_of(
        repo.versions("paro_epa", "ES", D(2023, 1, 1), D(2023, 6, 30)), D(2025, 1, 1)
    )
    assert [(v.observation.periodo, v.observation.valor) for v in values] == [
        ("2023T1", 13.3),
        ("2023T2", 11.6),
    ]


def test_estimated_publication_dates_are_flagged_in_the_note():
    est = row(
        6,
        5.0,
        "2026-10-14",
        1,
        vigente=True,
        fecha_publicacion_estimada=True,
        periodo="2026T2",
        periodo_inicio=D(2026, 4, 1),
        periodo_fin=D(2026, 6, 30),
    )
    [value] = resolve_as_of([est], D(2026, 10, 20))
    assert "fecha de publicación estimada" in value.note()


def test_rows_of_other_indicators_or_territories_are_not_returned():
    other = row(7, 1.0, "2023-04-28", 1, vigente=True, territorio_code="01")
    repo = InMemoryRepository([FIRST, REVISED, other])
    assert all(
        r.territorio_code == "ES"
        for r in repo.versions("paro_epa", "ES", D(2023, 1, 1), D(2023, 3, 31))
    )
    with pytest.raises(ValueError):
        resolve_as_of(
            [FIRST, row(8, 1.0, "2023-04-28", 1, vigente=True, indicator_id="ipc")], D(2024, 1, 1)
        )
