"""Acceso a las observaciones oficiales y resolución con fecha de referencia (as_of).

Las observaciones se versionan: una revisión del dato oficial es una fila nueva con su
fecha de publicación. Para verificar una afirmación se usa el valor **que se conocía en la
fecha del discurso** (la versión más reciente publicada hasta entonces); si después se
revisó, el valor vigente hoy se adjunta como nota. Solo se usan datos aprobados.

Dos implementaciones con la misma interfaz: Postgres (Supabase) y en memoria (tests y
copia local sin red).
"""

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import psycopg

Estado = Literal["pendiente", "cuarentena", "aprobado", "rechazado"]


@dataclass(frozen=True)
class SourceRow:
    organismo: str
    titulo: str
    url: str
    tipo: Literal["primaria", "secundaria"]
    fecha: dt.date | None = None


@dataclass(frozen=True)
class ObservationRow:
    id: int
    indicator_id: str
    periodo: str
    periodo_inicio: dt.date
    periodo_fin: dt.date
    territorio_code: str
    valor: float
    unidad: str
    fecha_publicacion: dt.date
    fecha_publicacion_estimada: bool
    version: int
    vigente: bool
    estado: Estado
    source: SourceRow


@dataclass(frozen=True)
class ResolvedValue:
    """The value known at the speech date, plus the current one if it was revised later."""

    observation: ObservationRow
    revised_later: ObservationRow | None = None

    def note(self) -> str:
        notes = []
        if self.revised_later is not None:
            r = self.revised_later
            notes.append(
                f"revisado posteriormente a {r.valor:g} {r.unidad} "
                f"(publicado el {r.fecha_publicacion:%d/%m/%Y})"
            )
        if self.observation.fecha_publicacion_estimada:
            notes.append("fecha de publicación estimada (la fuente no la indica)")
        return "; ".join(notes)


class KnowledgeRepository(Protocol):
    def versions(
        self, indicator_id: str, territorio: str, start: dt.date, end: dt.date
    ) -> list[ObservationRow]:
        """Every approved version of the periods overlapping [start, end]."""
        ...


def resolve_as_of(rows: Iterable[ObservationRow], as_of: dt.date) -> list[ResolvedValue]:
    """Per period: the latest version published on or before `as_of` (periods with nothing
    published by then are left out), in period order."""
    rows = list(rows)
    if len({(r.indicator_id, r.territorio_code) for r in rows}) > 1:
        raise ValueError("resolve_as_of recibe versiones de un solo indicador y territorio")
    by_period: dict[str, list[ObservationRow]] = {}
    for r in rows:
        if r.estado == "aprobado":
            by_period.setdefault(r.periodo, []).append(r)
    resolved = []
    for versions in by_period.values():
        known = [v for v in versions if v.fecha_publicacion <= as_of]
        if not known:
            continue
        chosen = max(known, key=lambda v: (v.fecha_publicacion, v.version))
        current = max(versions, key=lambda v: (v.fecha_publicacion, v.version))
        later = current if current.valor != chosen.valor else None
        resolved.append(ResolvedValue(chosen, later))
    return sorted(resolved, key=lambda v: v.observation.periodo_inicio)


class InMemoryRepository:
    def __init__(self, rows: Sequence[ObservationRow]) -> None:
        self._rows = list(rows)

    def versions(
        self, indicator_id: str, territorio: str, start: dt.date, end: dt.date
    ) -> list[ObservationRow]:
        return [
            r
            for r in self._rows
            if r.indicator_id == indicator_id
            and r.territorio_code == territorio
            and r.estado == "aprobado"
            and r.periodo_inicio <= end
            and r.periodo_fin >= start
        ]


_VERSIONS_SQL = """
select o.id, o.indicator_id, o.periodo, o.periodo_inicio, o.periodo_fin, o.territorio_code,
       o.valor::float8, o.unidad, o.fecha_publicacion, o.fecha_publicacion_estimada,
       o.version, o.vigente, o.estado,
       s.organismo, s.titulo, s.url, s.tipo, s.fecha
from ref.observations o
join ref.sources s on s.id = o.source_id
where o.indicator_id = %s and o.territorio_code = %s and o.estado = 'aprobado'
  and o.periodo_inicio <= %s and o.periodo_fin >= %s
order by o.periodo_inicio, o.version
"""


class PostgresRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def versions(
        self, indicator_id: str, territorio: str, start: dt.date, end: dt.date
    ) -> list[ObservationRow]:
        rows = self._conn.execute(_VERSIONS_SQL, (indicator_id, territorio, end, start))
        return [
            ObservationRow(
                *r[:13],
                source=SourceRow(organismo=r[13], titulo=r[14], url=r[15], tipo=r[16], fecha=r[17]),
            )
            for r in rows.fetchall()
        ]
