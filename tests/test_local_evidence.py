import datetime as dt

from pipeline.evidence.local import claim_period, local_evidence
from pipeline.indicators import Indicator, IndicatorBase
from pipeline.models import DateRange, DetectedClaim

D = dt.date
SRC = {"organismo": "INE", "documento": "EPA", "url": "https://www.ine.es/jaxiT3/Tabla.htm?t=65334"}
PARO = Indicator.model_validate(
    {
        "id": "paro_epa",
        "nombre": "Tasa de paro (EPA)",
        "unidad": "%",
        "periodicidad": "trimestral",
        "origen": "ine",
        "estado": "automatico",
        "actualizado": "2026-10-13",
        "serie": [
            {"desde": f"{y}-01-01", "hasta": f"{y}-12-31", "valor": v, "fuente": SRC}
            for y, v in ((2012, 25.8), (2013, 26.1), (2014, 24.4))
        ],
    }
)
BASE = IndicatorBase({"paro_epa": PARO})


def claim(**kw) -> DetectedClaim:
    base = {
        "id": "c001",
        "speaker": "s",
        "text_original": "el paro superó el 26 %",
        "text_normalized": "La tasa de paro EPA superó el 26 % en 2013.",
        "claim_type": "estadistica",
        "indicator": "paro_epa",
        "years": [2013],
    }
    return DetectedClaim(**base | kw)


def test_period_comes_from_resolved_context_first_then_years():
    scoped = claim(
        context_status="resuelto",
        time_scope=[DateRange(start=D(2012, 1, 1), end=D(2013, 6, 30))],
        years=[2020],
    )
    assert claim_period(scoped) == (D(2012, 1, 1), D(2013, 6, 30))
    assert claim_period(claim(years=[2013, 2012])) == (D(2012, 1, 1), D(2013, 12, 31))
    assert claim_period(claim(years=[])) is None


def test_covered_claim_gets_local_evidence():
    item = local_evidence(claim(), BASE)
    assert item is not None and item.source.kind == "indicador_local"
    assert "26,1" in item.excerpt


def test_no_local_evidence_without_indicator_period_or_coverage():
    assert local_evidence(claim(indicator=None), BASE) is None
    assert local_evidence(claim(years=[]), BASE) is None
    assert local_evidence(claim(years=[2020]), BASE) is None  # not covered: search the web
    assert local_evidence(claim(kind="promesa"), BASE) is None  # promises look at the future
