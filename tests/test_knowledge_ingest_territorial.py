"""Ingesta territorial (fase H), con respuestas del INE simuladas."""

import datetime as dt

import httpx
import pytest

from pipeline.knowledge.catalog import TerritorialSource, load_catalog, sync_catalog
from pipeline.knowledge.ingest import ingest_territorial, territorial_series
from pipeline.knowledge.territories import load_territories, sync_territories

from .test_knowledge_schema import db  # noqa: F401  (fixture)

TERRITORIES = load_territories()
SRC = TerritorialSource(
    nivel="ccaa", tabla=65334, filtro=["Tasa de paro de la población", "Ambos sexos", "Total"],
    rango_min=0, rango_max=60, salto_max_puntos=15,
)  # fmt: skip
PREFIX = "Tasa de paro de la población. Ambos sexos."
TABLE = [
    {"COD": "N", "Nombre": f"{PREFIX} Total Nacional. Total. "},
    {"COD": "AN", "Nombre": f"{PREFIX} Andalucía. Total. "},
    {"COD": "AS", "Nombre": f"{PREFIX} Asturias, Principado de. Total. "},
    {"COD": "J", "Nombre": f"{PREFIX} Andalucía. Menores de 25 años. "},
    {"COD": "X", "Nombre": f"{PREFIX} Atlántida. Total. "},
]


def _point(year: int, quarter: int, value: float) -> dict:
    return {"Anyo": year, "FK_Periodo": 18 + quarter, "Valor": value}


def handler(request: httpx.Request) -> httpx.Response:
    if "DATOS_TABLA" in request.url.path:
        return httpx.Response(200, json=TABLE)
    cod = request.url.path.rsplit("/", 1)[1]
    values = {"AN": [36.8, 36.0], "AS": [24.0, 23.5]}[cod]
    return httpx.Response(
        200, json={"Data": [_point(2013, 1, values[0]), _point(2013, 2, values[1])]}
    )


def test_each_series_is_matched_to_exactly_one_territory():
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        series, unmatched = territorial_series(SRC, client, TERRITORIES)
    assert [(cod, code) for cod, code, _ in series] == [("AN", "ES-AN"), ("AS", "ES-AS")]
    assert unmatched == [TABLE[4]["Nombre"]]  # reported, not guessed


@pytest.mark.supabase
def test_territorial_series_load_with_their_codes_and_level_limits(db):  # noqa: F811
    sync_territories(db, TERRITORIES)
    catalog = load_catalog()
    sync_catalog(db, catalog)
    spec = catalog["paro_epa"].model_copy(update={"territorial": [SRC]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = ingest_territorial(
            db, {"paro_epa": spec}, client, today=dt.date(2026, 10, 14), territories=TERRITORIES
        )
    [result] = report.results
    assert (result.status, result.summary.new, result.summary.quarantine) == ("cargado", 4, 0)
    rows = db.execute(
        "select territorio_code, periodo, valor::float8 from ref.observations"
        " where indicator_id = 'paro_epa' order by 1, 2"
    ).fetchall()
    assert rows[0] == ("ES-AN", "2013T1", 36.8) and {r[0] for r in rows} == {"ES-AN", "ES-AS"}
