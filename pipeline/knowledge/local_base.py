"""La base de conocimiento de Supabase vista como la base local de indicadores (fase E).

El análisis usa `IndicatorBase` (catálogo para la detección, extractos de series para la
evidencia). Aquí se construye desde las observaciones APROBADAS de ref.observations,
resueltas a la fecha del discurso: de cada periodo, la versión publicada hasta esa fecha
(`resolve_as_of`). Si el dato se revisó después, el extracto lo dice; si la fecha de
publicación es estimada por calendario, también. Cada valor lleva su id de observación
para la trazabilidad (`verif.claims.observation_ids`, `conocimiento impacto`).

Las observaciones se leen una vez (unos pocos miles de filas) y se resuelven en memoria.
"""

import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass, field

import psycopg
from pydantic import ValidationError

from pipeline.indicators import Indicator, IndicatorBase, IndicatorSource, Observation
from pipeline.knowledge.municipal import INDICATOR as MUNICIPAL_INDICATOR
from pipeline.knowledge.municipal import MUNICIPAL_PATH
from pipeline.knowledge.repository import ObservationRow, SourceRow, resolve_as_of

logger = logging.getLogger(__name__)

_ORIGIN = {
    "ine": "ine",
    "eurostat": "eurostat",
    "seguridad_social": "seguridad_social",
    "boe": "boe",
    "sepe": "sepe",
    "bde": "bde",
}
# Province codes are ES-P + two digits; ES-PV is the Basque Country (a region).
_PROVINCE = re.compile(r"ES-P\d{2}")
_PERIODICITY = {"puntual": "puntual", "irregular": "irregular"}


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    nombre: str
    definicion: str
    unidad: str
    frecuencia: str
    notas_metodologicas: str
    sinonimos: tuple[str, ...]
    conector: str


@dataclass
class KnowledgeIndicatorBase:
    catalog_entries: dict[str, CatalogEntry]
    rows: dict[str, list[ObservationRow]]
    # Territory code → name, for the scope of regional series.
    territory_names: dict[str, str] = field(default_factory=dict)
    # Problems building an indicator (kept out, reported, never silent).
    problems: list[str] = field(default_factory=list)
    _cache: dict[dt.date, IndicatorBase] = field(default_factory=dict, repr=False)

    @classmethod
    def from_db(cls, conn: psycopg.Connection) -> "KnowledgeIndicatorBase":
        catalog = {
            r[0]: CatalogEntry(r[0], r[1], r[2], r[3], r[4], r[5], tuple(r[6]), r[7])
            for r in conn.execute(
                "select id, nombre, definicion, unidad, frecuencia, notas_metodologicas,"
                " sinonimos, conector from ref.indicators order by id"
            ).fetchall()
        }
        rows: dict[str, list[ObservationRow]] = {}
        for r in conn.execute(
            "select o.id, o.indicator_id, o.periodo, o.periodo_inicio, o.periodo_fin,"
            " o.territorio_code, o.valor::float8, o.unidad, o.fecha_publicacion,"
            " o.fecha_publicacion_estimada, o.version, o.vigente, o.estado,"
            " s.organismo, s.titulo, s.url, s.tipo, s.fecha"
            " from ref.observations o join ref.sources s on s.id = o.source_id"
            " where o.estado = 'aprobado'"
            " order by o.indicator_id, o.territorio_code, o.periodo_inicio, o.version"
        ).fetchall():
            source = SourceRow(organismo=r[13], titulo=r[14], url=r[15], tipo=r[16], fecha=r[17])
            key = r[1] if r[5] == "ES" else f"{r[1]}@{r[5]}"
            rows.setdefault(key, []).append(ObservationRow(*r[:13], source=source))
        names = dict(conn.execute("select code, nombre from ref.territories").fetchall())
        return cls(catalog, rows, territory_names=names)

    def _indicator(
        self, entry: CatalogEntry, as_of: dt.date, key: str | None = None
    ) -> Indicator | None:
        """The series of `key` ("paro_epa" for Spain, "paro_epa@ES-AN" for a region)."""
        key = key or entry.id
        code = key.partition("@")[2]
        resolved = resolve_as_of(self.rows.get(key, []), as_of)
        if not resolved:
            return None
        serie = [
            Observation(
                desde=v.observation.periodo_inicio,
                hasta=v.observation.periodo_fin,
                valor=v.observation.valor,
                fuente=IndicatorSource(
                    organismo=v.observation.source.organismo,
                    documento=v.observation.source.titulo,
                    url=v.observation.source.url,
                    fecha=v.observation.source.fecha,
                ),
                observation_id=v.observation.id,
                nota=(
                    f"revisado posteriormente a {v.revised_later.valor:g}".replace(".", ",")
                    if v.revised_later is not None
                    else None
                ),
            )
            for v in resolved
        ]
        notes = [entry.definicion]
        if entry.notas_metodologicas:
            notes.append(entry.notas_metodologicas)
        estimated = sum(v.observation.fecha_publicacion_estimada for v in resolved)
        if estimated:
            notes.append(
                f"Valores publicados hasta el {as_of:%d/%m/%Y} (la fecha de publicación de"
                f" {estimated} de ellos está estimada por el calendario del organismo)."
            )
        try:
            return Indicator(
                id=entry.id,
                nombre=entry.nombre,
                unidad=entry.unidad,
                periodicidad=_PERIODICITY.get(entry.frecuencia, entry.frecuencia),
                origen=_ORIGIN.get(entry.conector, "manual"),  # type: ignore[arg-type]
                estado="revisado",
                ambito=self.territory_names.get(code, code) if code else "España",
                actualizado=as_of,  # what was known on the speech date
                notas=notes,
                serie=serie,
            )
        except ValidationError as exc:
            message = f"{entry.id}: no se pudo construir la serie ({exc.errors()[0]['msg']})"
            logger.warning(message)
            if message not in self.problems:
                self.problems.append(message)
            return None

    def at(self, as_of: dt.date) -> IndicatorBase:
        """The base as published on `as_of` (the speech date)."""
        if as_of in self._cache:
            return self._cache[as_of]
        indicators = {}
        for key in self.rows:
            entry = self.catalog_entries.get(key.partition("@")[0])
            if entry is None:
                continue
            ind = self._indicator(entry, as_of, key)
            if ind is not None:
                indicators[key] = ind
        self._cache[as_of] = IndicatorBase(indicators)
        return self._cache[as_of]

    def _today(self) -> IndicatorBase:
        return self.at(dt.date.today())

    @property
    def indicators(self) -> dict[str, Indicator]:
        return self._today().indicators

    def usable(self) -> list[Indicator]:
        return self._today().usable()

    def catalog(self) -> str:
        """One line per indicator with approved data: its synonyms and the scopes it has
        (Spain, regions, provinces), for the detection."""
        today = self._today().indicators
        lines = []
        for entry in self.catalog_entries.values():
            keys = [k for k in today if k.partition("@")[0] == entry.id]
            if not keys:
                continue
            scopes = []
            if entry.id in today:
                ind = today[entry.id]
                scopes.append(f"España {ind.cubre_desde.year}–{ind.cubre_hasta.year}")
            codes = [k.partition("@")[2] for k in keys if "@" in k]
            if any(_PROVINCE.fullmatch(c) for c in codes):
                scopes.append("por provincia")
            if any(not _PROVINCE.fullmatch(c) for c in codes):
                scopes.append("por comunidad autónoma")
            if entry.id == MUNICIPAL_INDICATOR and MUNICIPAL_PATH.is_file():
                scopes.append("por municipio")
            synonyms = f" Se nombra como: {', '.join(entry.sinonimos)}." if entry.sinonimos else ""
            lines.append(
                f"- {entry.id}: {entry.nombre} ({entry.unidad}; {'; '.join(scopes)}).{synonyms}"
            )
        return "\n".join(lines)

    def version(self, *, coverage: bool) -> str:
        """Detection depends on which indicators exist; evidence also on every approved
        row (a new value, revision or approval recomputes the step)."""
        ids = sorted(i.id for i in self.usable())
        parts = list(ids)
        if coverage:
            parts += [
                f"{k}:{len(v)}:{max(r.id for r in v)}" for k, v in sorted(self.rows.items()) if v
            ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12] if parts else ""
