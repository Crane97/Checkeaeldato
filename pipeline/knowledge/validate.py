"""Validación de los datos antes de entrar en ref.observations.

Un error en un dato se propagaría a todos los veredictos que lo usen, así que nada entra
sin pasar por aquí:
- esquema: unidad del catálogo, periodo coherente, valor numérico, sin duplicados;
- rango plausible del catálogo y salto respecto al periodo anterior → cuarentena;
- comparación con la versión vigente: igual → nada; distinto → revisión (versión nueva);
- estado: lo curado a mano entra pendiente; lo automático, aprobado si no hay motivos.
Las filas en cuarentena o pendientes nunca se usan para verificar.
"""

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pipeline.knowledge.catalog import IndicatorSpec

Action = Literal["nueva", "revision", "sin_cambios", "error"]


@dataclass(frozen=True)
class Candidate:
    """A value read from a source, before validation."""

    indicator_id: str
    periodo: str
    periodo_inicio: dt.date
    periodo_fin: dt.date
    valor: float
    unidad: str
    territorio_code: str = "ES"
    fecha_publicacion: dt.date | None = None  # None: the source does not give it


@dataclass(frozen=True)
class Current:
    """Latest stored version of a datum."""

    valor: float
    version: int


@dataclass
class Decision:
    candidate: Candidate
    action: Action
    estado: Literal["aprobado", "cuarentena", "pendiente"] = "aprobado"
    version: int = 1
    fecha_publicacion: dt.date | None = None
    fecha_publicacion_estimada: bool = False
    motivos: list[str] = field(default_factory=list)


def _jump(spec: IndicatorSpec, prev: float, value: float) -> str | None:
    if spec.salto_max_puntos is not None:
        diff = abs(value - prev)
        if diff > spec.salto_max_puntos:
            return (
                f"salto de {diff:.2f} puntos respecto al periodo anterior "
                f"(máx. {spec.salto_max_puntos:g})"
            )
        return None
    if prev == 0 or spec.salto_max_pct is None:
        return None
    pct = abs(value - prev) / abs(prev) * 100
    if pct > spec.salto_max_pct:
        return f"salto del {pct:.1f} % respecto al periodo anterior (máx. {spec.salto_max_pct:g} %)"
    return None


def validate_batch(
    candidates: Sequence[Candidate],
    spec: IndicatorSpec,
    current: dict[tuple[str, str], Current],
    *,
    today: dt.date,
    curated: bool = False,
    previous_values: dict[tuple[dt.date, str], float] | None = None,
) -> list[Decision]:
    """Decide what to do with each candidate of one indicator.

    `current`: latest stored version per (periodo, territorio). `previous_values`: stored
    values by (periodo_inicio, territorio), to measure the jump of the first new period.
    """
    keys = [(c.periodo, c.territorio_code) for c in candidates]
    duplicated = {k for k in keys if keys.count(k) > 1}
    known = dict(previous_values or {})
    decisions: list[Decision] = []

    for c in sorted(candidates, key=lambda c: (c.territorio_code, c.periodo_inicio)):
        d = Decision(candidate=c, action="nueva")
        if (c.periodo, c.territorio_code) in duplicated:
            d.action, d.motivos = "error", ["periodo duplicado en el lote"]
        elif c.unidad != spec.unidad:
            d.action, d.motivos = (
                "error",
                [f"unidad «{c.unidad}» distinta de la del catálogo «{spec.unidad}»"],
            )
        elif not isinstance(c.valor, int | float) or math.isnan(c.valor) or math.isinf(c.valor):
            d.action, d.motivos = "error", ["valor no numérico"]
        elif c.periodo_fin < c.periodo_inicio:
            d.action, d.motivos = "error", ["periodo con fin anterior al inicio"]
        if d.action == "error":
            decisions.append(d)
            continue

        stored = current.get((c.periodo, c.territorio_code))
        if stored is not None:
            if math.isclose(stored.valor, c.valor, rel_tol=0, abs_tol=1e-9):
                d.action = "sin_cambios"
                decisions.append(d)
                known[(c.periodo_inicio, c.territorio_code)] = c.valor
                continue
            d.action, d.version = "revision", stored.version + 1

        if spec.rango_min is not None and c.valor < spec.rango_min:
            d.motivos.append(f"valor por debajo del rango del catálogo ({spec.rango_min:g})")
        if spec.rango_max is not None and c.valor > spec.rango_max:
            d.motivos.append(f"valor por encima del rango del catálogo ({spec.rango_max:g})")
        prev_dates = [k[0] for k in known if k[1] == c.territorio_code and k[0] < c.periodo_inicio]
        if prev_dates:
            jump = _jump(spec, known[(max(prev_dates), c.territorio_code)], c.valor)
            if jump:
                d.motivos.append(jump)

        if c.fecha_publicacion is not None:
            d.fecha_publicacion = c.fecha_publicacion
        else:
            estimate = c.periodo_fin + dt.timedelta(days=spec.retraso_publicacion_dias)
            d.fecha_publicacion = min(estimate, today)  # we hold it today: published by now
            d.fecha_publicacion_estimada = True

        if curated:  # data that does not come from an official primary source
            d.estado = "pendiente"
        elif d.motivos:
            d.estado = "cuarentena"
        decisions.append(d)
        known[(c.periodo_inicio, c.territorio_code)] = c.valor

    return decisions
