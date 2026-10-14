import datetime as dt
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.indicators import (
    DEFAULT_INDICATORS_DIR,
    Indicator,
    IndicatorBase,
    load_indicators,
)

D = dt.date
BOE = {
    "organismo": "BOE",
    "documento": "Real Decreto 1077/2017",
    "url": "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2017-15848",
    "fecha": "2017-12-30",
}


def obs(desde: str, hasta: str, valor: float, **extra) -> dict:
    return {"desde": desde, "hasta": hasta, "valor": valor, "fuente": BOE, **extra}


def smi(**overrides) -> dict:
    data = {
        "id": "smi",
        "nombre": "Salario mínimo interprofesional",
        "unidad": "euros/mes (14 pagas)",
        "ambito": "España",
        "periodicidad": "anual",
        "estado": "revisado",
        "origen": "boe",
        "actualizado": "2026-10-13",
        "notas": ["Cuantía mensual en 14 pagas."],
        "serie": [
            obs("2017-01-01", "2017-12-31", 707.70),
            obs("2018-01-01", "2018-12-31", 735.90),
            obs("2019-01-01", "2019-12-31", 900),
        ],
    }
    return data | overrides


def base(*indicators: dict) -> IndicatorBase:
    return IndicatorBase({i["id"]: Indicator.model_validate(i) for i in indicators})


def test_coverage_is_the_span_of_the_series():
    ind = Indicator.model_validate(smi())
    assert (ind.cubre_desde, ind.cubre_hasta) == (D(2017, 1, 1), D(2019, 12, 31))
    assert ind.covers(D(2018, 6, 2), D(2019, 2, 21))
    assert not ind.covers(D(2016, 1, 1), D(2018, 1, 1))  # starts before the series
    assert not ind.covers(D(2019, 1, 1), D(2020, 1, 1))  # ends after it


def test_overlapping_or_unsorted_observations_are_rejected():
    overlapping = smi(
        serie=[obs("2018-01-01", "2018-12-31", 1), obs("2018-06-01", "2019-12-31", 2)]
    )
    with pytest.raises(ValidationError):
        Indicator.model_validate(overlapping)
    with pytest.raises(ValidationError):
        Indicator.model_validate(smi(serie=[obs("2018-12-31", "2018-01-01", 1)]))


def test_evidence_excerpt_has_values_sources_and_notes_for_the_period():
    item = base(smi()).evidence("smi", D(2018, 6, 2), D(2019, 2, 21))
    assert item is not None
    assert item.source.kind == "indicador_local"
    assert item.source.url.startswith("https://www.boe.es/")
    assert "735,9" in item.excerpt and "900" in item.excerpt
    assert "Real Decreto 1077/2017" in item.excerpt
    assert "14 pagas" in item.excerpt
    assert "707,7" in item.excerpt  # the value just before the period, for comparisons


def test_no_evidence_when_not_covered_unknown_or_pending_review():
    pending = smi(id="smi_pendiente", estado="pendiente_revision")
    b = base(smi(), pending)
    assert b.evidence("smi", D(2020, 1, 1), D(2020, 12, 31)) is None
    assert b.evidence("no_existe", D(2018, 1, 1), D(2018, 12, 31)) is None
    assert b.evidence("smi_pendiente", D(2018, 1, 1), D(2018, 12, 31)) is None
    assert [i.id for i in b.usable()] == ["smi"]


def test_load_from_directory(tmp_path: Path):
    (tmp_path / "smi.json").write_text(json.dumps(smi()))
    (tmp_path / "LEEME.md").write_text("no es un indicador")
    loaded = load_indicators(tmp_path)
    assert list(loaded.indicators) == ["smi"]


def test_file_name_must_match_the_id(tmp_path: Path):
    (tmp_path / "otro.json").write_text(json.dumps(smi()))
    with pytest.raises(ValueError, match="otro.json"):
        load_indicators(tmp_path)


def test_bundled_indicators_are_valid():
    load_indicators(DEFAULT_INDICATORS_DIR)  # every versioned file parses and validates
