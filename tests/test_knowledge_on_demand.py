"""Consulta a la API oficial bajo demanda (F1), sin red."""

import datetime as dt

import httpx

from pipeline.evidence.local import on_demand_evidence
from pipeline.indicators import Indicator, IndicatorSource, Observation
from pipeline.knowledge.catalog import load_catalog
from pipeline.knowledge.ingest import IngestError
from pipeline.knowledge.on_demand import OnDemandSeries
from pipeline.models import DetectedClaim

D = dt.date
CATALOG = load_catalog()
SPEC = CATALOG["deficit_publico_pib"]


def series(values: dict[int, float]) -> Indicator:
    src = IndicatorSource(
        organismo="Eurostat", documento="gov_10dd_edpt1", url="https://ec.europa.eu/x"
    )
    return Indicator(
        id=SPEC.id, nombre="x", unidad=SPEC.unidad, periodicidad="anual", origen="eurostat",
        estado="automatico", actualizado=D(2026, 10, 14),
        serie=[Observation(desde=D(y, 1, 1), hasta=D(y, 12, 31), valor=v, fuente=src)
               for y, v in sorted(values.items())],
    )  # fmt: skip


def on_demand(fetch):
    calls = []

    def counted(spec, client, today, since_year):
        calls.append(since_year)
        return fetch(spec, client, today, since_year)

    source = OnDemandSeries(
        CATALOG, lambda: httpx.Client(), today=D(2026, 10, 14), fetchers={"eurostat": counted}
    )
    return source, calls


def test_old_periods_come_validated_from_the_api_and_are_fetched_once():
    source, calls = on_demand(lambda *a: series({1995: -7.0, 1996: -4.9, 1997: -3.0, 1998: 45.0}))
    item = source.evidence("deficit_publico_pib", D(1995, 1, 1), D(1997, 12, 31))
    assert item.source.kind == "oficial" and "API oficial" in item.source.title
    assert "1995 -7" in item.excerpt.replace("−", "-") and "ACTUALES" in item.excerpt
    assert "Se descartaron 1 valores" in item.excerpt  # 1998: out of range → dropped
    source.evidence("deficit_publico_pib", D(1996, 1, 1), D(1996, 12, 31))
    assert calls == [1900]


def test_api_failures_are_recorded_not_hidden():
    def broken(*a):
        raise IngestError("Eurostat no responde")

    source, _ = on_demand(broken)
    assert source.evidence("deficit_publico_pib", D(1995, 1, 1), D(1995, 12, 31)) is None
    assert source.errors and "Eurostat no responde" in source.errors[0]


def test_manual_indicators_and_promises_never_go_to_the_api():
    source, calls = on_demand(lambda *a: series({1995: -7.0}))
    assert source.evidence("smi_mensual", D(1995, 1, 1), D(1995, 12, 31)) is None
    promise = DetectedClaim(
        id="p", speaker="A", text_original="x", text_normalized="x", claim_type="otro",
        kind="promesa", indicator="deficit_publico_pib", years=[1995],
    )  # fmt: skip
    assert on_demand_evidence(promise, source, D(2026, 1, 1)) is None
    assert calls == []
