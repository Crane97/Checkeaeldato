"""Golden tests: valores oficiales contrastados a mano con su fuente primaria (2026-10-14).

Si alguno falla, un cambio en la ingesta, la validación o el resolvedor ha alterado un dato
que se sabe correcto. No se corrige el test: se investiga el cambio.
"""

import datetime as dt
from typing import NamedTuple

import psycopg
import pytest

from pipeline.indicators import load_indicators
from pipeline.knowledge.repository import PostgresRepository, resolve_as_of


class Golden(NamedTuple):
    json_id: str
    indicator_id: str
    periodo: str
    day: dt.date  # a day inside the period
    valor: float
    fuente: str


GOLDEN = [
    Golden("smi", "smi_mensual", "2018", dt.date(2018, 6, 1), 735.90,
           "BOE-A-2017-15848 (RD 1077/2017): «735,9 euros/mes»"),
    Golden("smi", "smi_mensual", "2019", dt.date(2019, 6, 1), 900,
           "BOE-A-2018-17773 (RD 1462/2018): «900 euros/mes»"),
    Golden("smi", "smi_mensual", "2023", dt.date(2023, 6, 1), 1080,
           "BOE-A-2023-3982 (RD 99/2023): «1080 euros/mes»"),
    Golden("paro_epa", "paro_epa", "2013T1", dt.date(2013, 2, 1), 26.94,
           "INE, serie EPA452434, 2013T1"),
    Golden("ipc_interanual", "ipc_interanual", "2022-07", dt.date(2022, 7, 15), 10.8,
           "INE, serie IPC290750, julio de 2022"),
    Golden("deficit_pib", "deficit_publico_pib", "2020", dt.date(2020, 6, 1), -9.9,
           "Eurostat gov_10dd_edpt1, B9, % del PIB, 2020"),
    Golden("poblacion", "poblacion", "2024-01-01", dt.date(2024, 1, 1), 48_619_695,
           "INE, serie ECP320, 1 de enero de 2024"),
]  # fmt: skip
IDS = [f"{g.indicator_id}-{g.periodo}" for g in GOLDEN]


@pytest.mark.parametrize("g", GOLDEN, ids=IDS)
def test_json_base_holds_the_official_value(g: Golden):
    serie = load_indicators().indicators[g.json_id].serie
    [obs] = [o for o in serie if o.desde <= g.day <= o.hasta]
    assert obs.valor == pytest.approx(g.valor), g.fuente


@pytest.fixture
def loaded_db(supabase_url: str):
    """The development database as loaded, read-only."""
    with psycopg.connect(supabase_url, connect_timeout=20) as conn:
        conn.read_only = True
        count = conn.execute("select count(*) from ref.observations").fetchone()[0]
        if not count:
            pytest.skip("La base de desarrollo no tiene observaciones cargadas")
        yield conn
        conn.rollback()


@pytest.mark.supabase
@pytest.mark.parametrize("g", GOLDEN, ids=IDS)
def test_database_holds_the_official_value(loaded_db, g: Golden):
    row = loaded_db.execute(
        "select valor::float8, estado, fecha_publicacion from ref.observations"
        " where indicator_id = %s and periodo = %s and territorio_code = 'ES' and vigente",
        (g.indicator_id, g.periodo),
    ).fetchone()
    assert row is not None, g
    valor, estado, published = row
    assert valor == pytest.approx(g.valor), g.fuente
    if estado != "aprobado":
        return  # curated data still waiting for review: the resolver must not see it
    repo = PostgresRepository(loaded_db)
    versions = repo.versions(g.indicator_id, "ES", g.day, g.day)
    [after] = resolve_as_of(versions, published)
    assert after.observation.valor == pytest.approx(g.valor)
    assert resolve_as_of(versions, published - dt.timedelta(days=1)) == []  # not yet known


@pytest.mark.supabase
def test_pending_data_is_invisible_to_the_resolver(loaded_db):
    smi = [g for g in GOLDEN if g.indicator_id == "smi_mensual"][0]
    estado = loaded_db.execute(
        "select estado from ref.observations where indicator_id = 'smi_mensual'"
        " and periodo = %s and vigente",
        (smi.periodo,),
    ).fetchone()[0]
    if estado == "aprobado":
        pytest.skip("El SMI ya está revisado")
    assert PostgresRepository(loaded_db).versions("smi_mensual", "ES", smi.day, smi.day) == []
