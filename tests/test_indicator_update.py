import datetime as dt
import json

import httpx
import respx

from pipeline.indicator_update import (
    EUROSTAT_URL,
    INE_URL,
    SS_URL,
    EurostatSpec,
    IneSpec,
    build_affiliation,
    build_eurostat,
    build_ine,
    parse_ss_csv,
)

D = dt.date


def ine_point(year: int, month: int, value: float, fk: int) -> dict:
    ms = int(dt.datetime(year, month, 1, tzinfo=dt.UTC).timestamp() * 1000)
    return {"Fecha": ms, "Anyo": year, "FK_Periodo": fk, "Valor": value}


@respx.mock
def test_ine_quarterly_series_becomes_quarters_with_their_source():
    respx.get(INE_URL.format(cod="EPA452434")).mock(
        return_value=httpx.Response(
            200,
            json={
                "Nombre": "Tasa de paro. Total Nacional.",
                "Data": [ine_point(2023, 1, 13.26, 19), ine_point(2023, 4, 11.6, 20)],
            },
        )
    )
    spec = IneSpec(
        id="paro_epa",
        nombre="Tasa de paro (EPA)",
        unidad="% de la población activa",
        cod="EPA452434",
        tabla=65334,
        documento="Encuesta de Población Activa (EPA)",
        periodicidad="trimestral",
        notas=["Paro según la EPA, no paro registrado."],
    )
    ind = build_ine(spec, httpx.Client(), today=D(2026, 10, 13))
    assert [(o.desde, o.hasta, o.valor) for o in ind.serie] == [
        (D(2023, 1, 1), D(2023, 3, 31), 13.26),
        (D(2023, 4, 1), D(2023, 6, 30), 11.6),
    ]
    assert ind.estado == "automatico" and ind.origen == "ine"
    assert "t=65334" in ind.serie[0].fuente.url
    assert "EPA452434" in ind.serie[0].fuente.documento


@respx.mock
def test_ine_point_in_time_series_is_one_day():
    respx.get(INE_URL.format(cod="ECP320")).mock(
        return_value=httpx.Response(200, json={"Data": [ine_point(2025, 1, 48_600_000, 19)]})
    )
    spec = IneSpec(
        id="poblacion",
        nombre="Población residente",
        unidad="personas",
        cod="ECP320",
        tabla=56934,
        documento="Estadística Continua de Población",
        periodicidad="puntual",
    )
    [o] = build_ine(spec, httpx.Client(), today=D(2026, 10, 13)).serie
    assert (o.desde, o.hasta) == (D(2025, 1, 1), D(2025, 1, 1))


@respx.mock
def test_eurostat_annual_values_in_time_order():
    payload = {
        "value": {"0": -7.0, "1": -4.6},
        "dimension": {"time": {"category": {"index": {"2021": 0, "2022": 1}}}},
        "label": "Government deficit",
    }
    route = respx.get(url__startswith=EUROSTAT_URL).mock(
        return_value=httpx.Response(200, json=payload)
    )
    spec = EurostatSpec(
        id="deficit_pib",
        nombre="Déficit público",
        unidad="% del PIB",
        dataset="gov_10dd_edpt1",
        filtros={"unit": "PC_GDP", "sector": "S13", "na_item": "B9"},
        notas=["Negativo = déficit."],
    )
    ind = build_eurostat(spec, httpx.Client(), today=D(2026, 10, 13))
    assert [(o.desde.year, o.valor) for o in ind.serie] == [(2021, -7.0), (2022, -4.6)]
    assert ind.origen == "eurostat"
    assert "geo=ES" in str(route.calls[0].request.url)


SS_CSV = (
    '"Afiliados medios por Periodo, Sector y ASALARIADOS"\r\n\r\n'
    '"Periodo";"Sector";"Total";"Asalariados";"No Asalariados"\r\n'
    '"202012";"Total";19000000.40;15700000.00;3300000.40\r\n'
    '"202012";"Industria";2700000.00;2400000.00;300000.00\r\n'
    '"202101";"Total";18900000.60;15600000.00;3300000.60\r\n'
).encode("cp1252")


def test_parse_ss_csv_keeps_the_monthly_totals():
    assert parse_ss_csv(SS_CSV) == {(2020, 12): 19000000.40, (2021, 1): 18900000.60}


@respx.mock
def test_affiliation_joins_both_classifications_and_checks_the_overlap():
    old = SS_CSV
    new = SS_CSV.replace(b'"202012";"Total";19000000.40', b'"202012";"Total";19000001.00')
    respx.get(SS_URL.format(query="cnae09")).mock(return_value=httpx.Response(200, content=old))
    respx.get(SS_URL.format(query="cnae25")).mock(return_value=httpx.Response(200, content=new))
    ind = build_affiliation(httpx.Client(), today=D(2026, 10, 13), queries=("cnae09", "cnae25"))
    assert [(o.desde, o.valor) for o in ind.serie] == [
        (D(2020, 12, 1), 19000001),  # the current classification (CNAE-25) wins
        (D(2021, 1, 1), 18900001),
    ]
    assert all(o.fuente.organismo.startswith("Tesorería") for o in ind.serie)


def test_specs_cover_every_requested_indicator():
    from pipeline.indicator_update import SPECS

    ids = {s.id for s in SPECS}
    assert {
        "paro_epa",
        "paro_juvenil_epa",
        "ipc_interanual",
        "poblacion",
        "pib",
        "pib_crecimiento",
        "deuda_pib",
        "deuda_absoluta",
        "deficit_pib",
    } <= ids
    assert json.dumps([s.id for s in SPECS])  # serializable ids


@respx.mock
def test_ine_points_without_date_use_year_and_period_and_old_ones_are_dropped():
    respx.get(INE_URL.format(cod="IPC290750")).mock(
        return_value=httpx.Response(
            200,
            json={
                "Data": [
                    {"Anyo": 1962, "FK_Periodo": 1, "Valor": 0.4},  # before SINCE_YEAR
                    {"Anyo": 2024, "FK_Periodo": 3, "Valor": 3.2},
                ]
            },
        )
    )
    spec = IneSpec(
        id="ipc_interanual",
        nombre="IPC",
        unidad="%",
        cod="IPC290750",
        tabla=76125,
        documento="IPC",
        periodicidad="mensual",
    )
    [o] = build_ine(spec, httpx.Client(), today=D(2026, 10, 13)).serie
    assert (o.desde, o.hasta, o.valor) == (D(2024, 3, 1), D(2024, 3, 31), 3.2)
