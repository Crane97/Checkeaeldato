"""Base local de indicadores oficiales (data/knowledge/indicators/).

Series de datos que se repiten en casi todos los debates (SMI, paro EPA, IPC, deuda,
afiliación, pensiones…), con la fuente primaria de cada dato. Si una afirmación se refiere
a uno de estos indicadores y su periodo está cubierto, la evidencia sale de aquí
(`indicador_local`) y no se gasta una búsqueda web.

Las series de la API del INE y de Eurostat se generan con scripts/actualizar_indicadores.py;
las que se copian a mano de documentos oficiales (BOE, Seguridad Social) llegan como
`pendiente_revision` y no se usan hasta que el responsable del proyecto las revisa.
"""

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from pipeline.models import EvidenceItem, Source

DEFAULT_INDICATORS_DIR = Path(__file__).resolve().parent.parent / "data/knowledge/indicators"
# Values shown around the claim's period, so comparisons with the previous value work.
CONTEXT_OBSERVATIONS = 1
MAX_EXCERPT_OBSERVATIONS = 40


class IndicatorSource(BaseModel):
    organismo: str
    documento: str
    url: str
    fecha: dt.date | None = None
    id: str | None = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        return v


class Observation(BaseModel):
    desde: dt.date
    hasta: dt.date
    valor: float
    fuente: IndicatorSource
    provisional: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.hasta < self.desde:
            raise ValueError(
                f"observación con hasta ({self.hasta}) anterior a desde ({self.desde})"
            )
        return self


class Indicator(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    nombre: str
    unidad: str
    ambito: str = "España"
    periodicidad: str
    origen: Literal["ine", "eurostat", "seguridad_social", "boe"]
    # "pendiente_revision": copied by hand, not used until reviewed.
    estado: Literal["revisado", "automatico", "pendiente_revision"]
    actualizado: dt.date
    notas: list[str] = Field(default_factory=list)
    serie: list[Observation] = Field(min_length=1)

    @model_validator(mode="after")
    def _sorted_without_overlaps(self) -> Self:
        for prev, cur in zip(self.serie, self.serie[1:], strict=False):
            if cur.desde <= prev.hasta:
                raise ValueError(
                    f"{self.id}: observaciones desordenadas o solapadas ({prev.desde}–{prev.hasta}"
                    f" y {cur.desde}–{cur.hasta})"
                )
        return self

    @property
    def cubre_desde(self) -> dt.date:
        return self.serie[0].desde

    @property
    def cubre_hasta(self) -> dt.date:
        return self.serie[-1].hasta

    @property
    def usable(self) -> bool:
        return self.estado != "pendiente_revision"

    def covers(self, start: dt.date, end: dt.date) -> bool:
        return self.cubre_desde <= start and end <= self.cubre_hasta

    def window(self, start: dt.date, end: dt.date) -> list[Observation]:
        """Observations overlapping [start, end] plus the one just before, for comparisons."""
        inside = [i for i, o in enumerate(self.serie) if o.desde <= end and o.hasta >= start]
        if not inside:
            return []
        first = max(inside[0] - CONTEXT_OBSERVATIONS, 0)
        chosen = self.serie[first : inside[-1] + 1]
        if len(chosen) > MAX_EXCERPT_OBSERVATIONS:  # long monthly series: keep the edges
            half = MAX_EXCERPT_OBSERVATIONS // 2
            chosen = chosen[:half] + chosen[-half:]
        return chosen


def _number(value: float) -> str:
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", "§").replace(".", ",").replace("§", ".")


def _period(o: Observation) -> str:
    if o.desde.day == 1 and o.desde.month == 1 and o.hasta.month == 12 and o.hasta.day == 31:
        return (
            str(o.desde.year) if o.desde.year == o.hasta.year else f"{o.desde.year}–{o.hasta.year}"
        )
    return f"{o.desde:%d/%m/%Y}–{o.hasta:%d/%m/%Y}"


@dataclass(frozen=True)
class IndicatorBase:
    indicators: dict[str, Indicator]

    def usable(self) -> list[Indicator]:
        return [i for i in self.indicators.values() if i.usable]

    def version(self, *, coverage: bool) -> str:
        """Short hash of what the pipeline uses: the usable ids (detection) and, with
        `coverage`, how far each series goes (evidence). Changing it recomputes the step."""
        parts = [
            f"{i.id}:{i.cubre_hasta.isoformat()}" if coverage else i.id
            for i in sorted(self.usable(), key=lambda i: i.id)
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12] if parts else ""

    def catalog(self) -> str:
        """One line per usable indicator, for the detection prompt."""
        return "\n".join(
            f"- {i.id}: {i.nombre} ({i.unidad}; {i.ambito}; {i.cubre_desde.year}–"
            f"{i.cubre_hasta.year})"
            for i in self.usable()
        )

    def evidence(self, indicator_id: str, start: dt.date, end: dt.date) -> EvidenceItem | None:
        """Local evidence for [start, end], or None if unknown, unreviewed or not covered."""
        ind = self.indicators.get(indicator_id)
        if ind is None or not ind.usable or not ind.covers(start, end):
            return None
        rows = ind.window(start, end)
        lines = [
            f"{_period(o)}: {_number(o.valor)} ({o.fuente.documento}"
            f"{', provisional' if o.provisional else ''})"
            for o in rows
        ]
        excerpt = (
            f"{ind.nombre}, {ind.ambito}, en {ind.unidad}. "
            + " | ".join(lines)
            + ". Notas: "
            + " ".join(ind.notas)
        )
        last = rows[-1].fuente
        return EvidenceItem(
            id="",
            source=Source(
                url=last.url,
                title=f"{ind.nombre} (serie oficial, base local de indicadores)",
                organization=last.organismo,
                date=ind.actualizado,
                kind="indicador_local",
            ),
            excerpt=excerpt,
        )


def load_indicators(directory: Path = DEFAULT_INDICATORS_DIR) -> IndicatorBase:
    indicators: dict[str, Indicator] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            ind = Indicator.model_validate(json.loads(path.read_text()))
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"Indicador inválido en {path.name}: {exc}") from exc
        if path.stem != ind.id:
            raise ValueError(f"{path.name}: el nombre del archivo debe ser {ind.id}.json")
        indicators[ind.id] = ind
    return IndicatorBase(indicators)
