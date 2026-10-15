"""Población municipal del Padrón en Parquet (fase H4)."""

import datetime as dt
from pathlib import Path

from pipeline.knowledge.municipal import MunicipalBase, parse_padron_csv, write_parquet
from pipeline.knowledge.territories import load_territories

D = dt.date
CSV = (
    "﻿Municipios;Sexo;Periodo;Total\n"
    "28065 Getafe;Total;2024;189.906\n28065 Getafe;Hombres;2024;92.000\n"
    "28065 Getafe;Total;2023;185.899\n"
    "06083 Mérida;Total;2024;60.119\n"
    "41001 Villanueva;Total;2024;1.000\n42001 Villanueva;Total;2024;2.000\n"
    "35016 Palmas de Gran Canaria, Las;Total;2024;380.000\n"
    "99999 Roto;Total;2024;..\n"
)


def base() -> MunicipalBase:
    rows = parse_padron_csv(CSV, 2881)
    return MunicipalBase(rows, {"06": {"badajoz"}, "28": {"madrid"}})


def test_only_totals_with_codes_and_numbers_are_read():
    rows = parse_padron_csv(CSV, 2881)
    assert {(r["municipio_ine"], r["anyo"], r["valor"]) for r in rows} >= {
        ("28065", 2024, 189906), ("28065", 2023, 185899)
    }  # fmt: skip
    assert all(r["municipio_ine"] != "99999" for r in rows)
    assert not any(r["valor"] == 92000 for r in rows)  # men only


def test_parquet_round_trip(tmp_path: Path):
    path = tmp_path / "padron.parquet"
    write_parquet(parse_padron_csv(CSV, 2881), path)
    loaded = MunicipalBase.load(path, load_territories())
    assert loaded.resolve("Getafe") == "ES-M28065"


def test_names_resolve_only_when_unique_or_narrowed_by_province():
    b = base()
    assert b.resolve("el municipio de Getafe") == "ES-M28065"
    assert b.resolve("Las Palmas de Gran Canaria") == "ES-M35016"
    assert b.resolve("Villanueva") is None
    assert b.resolve("Mérida (Badajoz)") == "ES-M06083"


def test_only_figures_published_by_the_speech_date_are_used():
    b = base()
    early = b.evidence("ES-M28065", [2024], D(2024, 6, 1))  # 2024 published ~December
    assert early is None
    later = b.evidence("ES-M28065", [2024], D(2025, 3, 1))
    assert "1/1/2024 189.906" in later.excerpt and "1/1/2023 185.899" in later.excerpt
    assert later.source.kind == "indicador_local"


def test_names_shared_with_a_region_or_province_are_not_taken_for_a_city():
    from pipeline.claims import ExtractedClaim, _territory_code

    def item(territory: str) -> ExtractedClaim:
        return ExtractedClaim(
            segment=0, quote="x", text_normalized="x", claim_type="estadistica",
            search_queries=[], speaker="", needs_context=False, territory=territory,
        )  # fmt: skip

    assert _territory_code(item("Madrid")) is None
    assert _territory_code(item("Andalucía")) == "ES-AN"
