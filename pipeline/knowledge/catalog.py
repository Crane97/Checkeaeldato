"""Catálogo de indicadores (catalog.yaml) y su sincronización con ref.indicators."""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

import psycopg
import yaml
from pydantic import BaseModel, Field, model_validator

CATALOG_PATH = Path(__file__).resolve().parent / "catalog.yaml"

Tema = Literal[
    "economia",
    "empleo",
    "pensiones",
    "vivienda",
    "sanidad",
    "educacion",
    "inmigracion",
    "medio_ambiente",
    "energia",
    "seguridad",
    "fiscalidad",
    "instituciones",
    "otros",
]
Frecuencia = Literal["mensual", "trimestral", "anual", "puntual", "irregular"]
Conector = Literal["ine", "eurostat", "seguridad_social", "sepe", "bde", "boe", "manual"]


class IndicatorSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    nombre: str
    definicion: str
    unidad: str
    frecuencia: Frecuencia
    organismo: str
    notas_metodologicas: str = ""
    sinonimos: list[str] = Field(min_length=1)
    rango_min: float | None = None
    rango_max: float | None = None
    salto_max_pct: float | None = Field(default=None, gt=0)
    salto_max_puntos: float | None = Field(default=None, gt=0)
    max_antiguedad_dias: int = Field(gt=0)
    tema: Tema
    retraso_publicacion_dias: int = Field(ge=0)
    conector: Conector
    codigo_fuente: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.salto_max_pct is None) == (self.salto_max_puntos is None):
            raise ValueError(f"{self.id}: hace falta exactamente un umbral de salto (% o puntos)")
        if (
            self.rango_min is not None
            and self.rango_max is not None
            and self.rango_min > self.rango_max
        ):
            raise ValueError(f"{self.id}: rango_min mayor que rango_max")
        return self


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, IndicatorSpec]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    catalog: dict[str, IndicatorSpec] = {}
    for raw in data["indicadores"]:
        spec = IndicatorSpec.model_validate(raw)
        if spec.id in catalog:
            raise ValueError(f"Indicador duplicado en el catálogo: {spec.id}")
        catalog[spec.id] = spec
    return catalog


@dataclass
class SyncReport:
    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


# Constant column names (never user input), in IndicatorSpec order.
_COLUMNS = (
    "nombre",
    "definicion",
    "unidad",
    "frecuencia",
    "organismo",
    "notas_metodologicas",
    "sinonimos",
    "rango_min",
    "rango_max",
    "salto_max_pct",
    "salto_max_puntos",
    "max_antiguedad_dias",
    "tema",
    "retraso_publicacion_dias",
    "conector",
    "codigo_fuente",
)


def sync_catalog(conn: psycopg.Connection, catalog: dict[str, IndicatorSpec]) -> SyncReport:
    """Insert new indicators and update the rest. Unit, definition and frequency cannot
    change (the database refuses it): such a change needs a new indicator id."""
    report = SyncReport()
    cols = ", ".join(_COLUMNS)
    for spec in catalog.values():
        values = [getattr(spec, c) for c in _COLUMNS]
        row = conn.execute(
            f"select {cols} from ref.indicators where id = %s", (spec.id,)
        ).fetchone()
        if row is None:
            marks = ", ".join(["%s"] * (len(_COLUMNS) + 1))
            conn.execute(
                f"insert into ref.indicators (id, {cols}) values ({marks})", [spec.id, *values]
            )
            report.new.append(spec.id)
        elif [_plain(v) for v in row] == [_plain(v) for v in values]:
            report.unchanged.append(spec.id)
        else:
            sets = ", ".join(f"{c} = %s" for c in _COLUMNS)
            try:
                conn.execute(f"update ref.indicators set {sets} where id = %s", [*values, spec.id])
            except psycopg.errors.RestrictViolation as exc:
                raise ValueError(f"Indicador {spec.id}: {exc.diag.message_primary}") from exc
            report.updated.append(spec.id)
    return report


def _plain(value: object) -> object:
    """Compare Decimal from the database with floats from the YAML (only Decimals: a text
    such as "nan" must not become a float)."""
    return float(value) if isinstance(value, Decimal) else value
