"""Catálogo y validación de los datos antes de entrar en la base."""

import datetime as dt
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.knowledge.catalog import CATALOG_PATH, IndicatorSpec, load_catalog
from pipeline.knowledge.validate import Candidate, Current, validate_batch

D = dt.date
REQUESTED = {
    "smi_mensual",
    "paro_epa",
    "paro_juvenil_epa",
    "ocupados_epa",
    "paro_registrado",
    "afiliacion_ss",
    "ipc_interanual",
    "deuda_publica_pib",
    "deuda_publica_absoluta",
    "deficit_publico_pib",
    "num_pensiones",
    "num_pensionistas",
    "pension_media",
    "poblacion",
    "pib_crecimiento",
    "precio_vivienda",
    "iva_general",
    "turistas_internacionales",
    "edad_emancipacion",
}


def test_catalog_has_every_requested_indicator_with_a_definition():
    catalog = load_catalog()
    assert set(catalog) >= REQUESTED
    for spec in catalog.values():
        assert len(spec.definicion) > 40 and spec.sinonimos, spec.id
        assert (spec.salto_max_pct is None) != (spec.salto_max_puntos is None), spec.id


def test_rate_indicators_measure_jumps_in_points():
    catalog = load_catalog()
    for spec in catalog.values():
        if spec.unidad.startswith("%"):
            assert spec.salto_max_puntos is not None, spec.id


def test_a_spec_needs_exactly_one_jump_threshold(tmp_path: Path):
    data = load_catalog()["paro_epa"].model_dump()
    with pytest.raises(ValidationError):
        IndicatorSpec(**data | {"salto_max_pct": 10})
    with pytest.raises(ValidationError):
        IndicatorSpec(**data | {"salto_max_puntos": None})


def test_duplicated_ids_are_rejected(tmp_path: Path):
    text = CATALOG_PATH.read_text(encoding="utf-8")
    first = text.index("  - id: paro_epa")
    second = text.index("  - id:", first + 1)
    path = tmp_path / "catalog.yaml"
    path.write_text(text + text[first:second], encoding="utf-8")
    with pytest.raises(ValueError, match="paro_epa"):
        load_catalog(path)


# --- validation -------------------------------------------------------------

PARO = load_catalog()["paro_epa"]
SMI = load_catalog()["smi_mensual"]
TODAY = D(2026, 10, 14)


def q(year: int, quarter: int, valor: float, **kw) -> Candidate:
    start = D(year, 3 * quarter - 2, 1)
    end = (D(year, 3 * quarter + 1, 1) if quarter < 4 else D(year + 1, 1, 1)) - dt.timedelta(1)
    base = {
        "indicator_id": "paro_epa",
        "periodo": f"{year}T{quarter}",
        "periodo_inicio": start,
        "periodo_fin": end,
        "valor": valor,
        "unidad": PARO.unidad,
    }
    return Candidate(**base | kw)


def check(candidates, spec=PARO, current=None, curated=False):
    return validate_batch(candidates, spec, current or {}, today=TODAY, curated=curated)


def test_plausible_automatic_data_is_approved_with_an_estimated_date():
    [d] = check([q(2023, 1, 13.26)])
    assert (d.action, d.estado, d.motivos) == ("nueva", "aprobado", [])
    assert d.fecha_publicacion == D(2023, 3, 31) + dt.timedelta(days=PARO.retraso_publicacion_dias)
    assert d.fecha_publicacion_estimada


def test_real_publication_dates_are_kept():
    [d] = check([q(2023, 1, 13.26, fecha_publicacion=D(2023, 4, 28))])
    assert d.fecha_publicacion == D(2023, 4, 28) and not d.fecha_publicacion_estimada


def test_estimated_dates_never_go_beyond_the_ingestion_day():
    [d] = check([q(2026, 3, 10.0)])  # quarter ends 2026-09-30; estimate would be 2026-10-28
    assert d.fecha_publicacion == TODAY and d.fecha_publicacion_estimada


def test_out_of_range_values_go_to_quarantine():
    [d] = check([q(2023, 1, 55.0)])
    assert d.estado == "cuarentena" and any("rango" in m for m in d.motivos)


def test_jumps_are_measured_against_the_previous_period():
    prev = q(2022, 4, 12.9)
    decisions = check([prev, q(2023, 1, 13.3), q(2023, 2, 25.0)])
    assert [d.estado for d in decisions] == ["aprobado", "aprobado", "cuarentena"]
    assert any("salto" in m and "puntos" in m for m in decisions[2].motivos)


def test_relative_jumps_for_levels():
    def smi(year: int, valor: float) -> Candidate:
        return Candidate(
            indicator_id="smi_mensual",
            periodo=str(year),
            periodo_inicio=D(year, 1, 1),
            periodo_fin=D(year, 12, 31),
            valor=valor,
            unidad=SMI.unidad,
            fecha_publicacion=D(year - 1, 12, 30),
        )

    decisions = check([smi(2018, 735.9), smi(2019, 900), smi(2020, 1500)], SMI)
    assert [d.estado for d in decisions] == ["aprobado", "aprobado", "cuarentena"]  # +22 %, +67 %


def test_curated_data_always_enters_pending():
    [d] = check([q(2023, 1, 13.26)], curated=True)
    assert d.estado == "pendiente"


def test_unchanged_values_are_skipped_and_changes_are_revisions():
    current = {("2023T1", "ES"): Current(valor=13.26, version=1)}
    same = check([q(2023, 1, 13.26), q(2023, 2, 11.6)], current=current)[0]
    assert same.action == "sin_cambios"
    [rev] = check([q(2023, 1, 13.3)], current=current)
    assert (rev.action, rev.version) == ("revision", 2)


def test_schema_errors_wrong_unit_nan_and_duplicates():
    wrong_unit, nan = check([q(2023, 1, 13.0, unidad="%"), q(2023, 2, float("nan"))])
    assert wrong_unit.action == "error" and any("unidad" in m for m in wrong_unit.motivos)
    assert nan.action == "error"
    dup = check([q(2023, 1, 13.0), q(2023, 1, 13.1)])
    assert all(d.action == "error" and any("duplicad" in m for m in d.motivos) for d in dup)


def test_synced_columns_are_spec_fields():
    from pipeline.knowledge.catalog import _COLUMNS

    assert set(_COLUMNS) == set(IndicatorSpec.model_fields) - {"id"}
