"""Base de políticos: nombre, alias, nacimiento, cargos y partidos fechados, con fuente.

La consulta el paso `speaker_research` antes de buscar en la web: si el orador está y se
sabe su partido en la fecha del discurso, no se gasta ninguna búsqueda. Si no está y la
investigación lo identifica, se guarda (caché local y, si está configurada, la base de
Supabase) como investigación automática: se reutiliza enseguida y, si una persona la
rechaza en `conocimiento revisar`, deja de usarse.

Solo se usan los políticos `aprobado` (curados y revisados) y `automatico` (investigados);
nunca los `pendiente` ni los `rechazado`.
"""

import datetime as dt
import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

Estado = Literal["pendiente", "automatico", "aprobado", "rechazado"]
USABLE: frozenset[str] = frozenset({"aprobado", "automatico"})
# A party or office seen in a research result (no start date) is reused for speeches up to
# this many days away from the date it was seen; beyond that it is researched again.
SNAPSHOT_DAYS = 365
DEFAULT_CACHE = Path("data/cache/politicians.json")

_HONORIFICS_RE = re.compile(
    r"^(?:(?:sr|sra|srta|d|dña|dr|dra)\.?|señor|señora|don|doña)\s+", re.IGNORECASE
)
_WORD_RE = re.compile(r"\w+")
_CONNECTORS = {"de", "del", "la", "las", "los", "y", "i"}


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip()


def name_words(name: str) -> list[str]:
    plain = _HONORIFICS_RE.sub("", name.strip())
    return [w for w in _WORD_RE.findall(fold(plain)) if w not in _CONNECTORS]


def slug(name: str) -> str:
    return "-".join(name_words(name)) or "sin-nombre"


class PoliticianSource(BaseModel):
    organismo: str
    titulo: str
    url: str = Field(pattern=r"^https?://")
    fecha: dt.date | None = None
    tipo: Literal["primaria", "secundaria"] = "primaria"


class _Dated(BaseModel):
    inicio: dt.date | None = None
    fin: dt.date | None = None
    constatado_en: dt.date | None = None
    source: PoliticianSource

    @model_validator(mode="after")
    def _has_date(self):
        if (self.inicio is None) == (self.constatado_en is None):
            raise ValueError("hace falta inicio o constatado_en (solo uno de los dos)")
        if self.constatado_en is not None and self.fin is not None:
            raise ValueError("un dato constatado no tiene fin")
        if self.inicio and self.fin and self.fin < self.inicio:
            raise ValueError("fin anterior a inicio")
        return self

    def holds_on(self, day: dt.date) -> bool:
        if self.inicio is not None:
            return self.inicio <= day and (self.fin is None or day <= self.fin)
        assert self.constatado_en is not None
        return abs((day - self.constatado_en).days) <= SNAPSHOT_DAYS


class Role(_Dated):
    cargo: str
    organo: str | None = None
    territorio: str = "ES"
    en_funciones: bool = False


class Membership(_Dated):
    partido: str


class Politician(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    nombre: str
    alias: list[str] = Field(default_factory=list)
    fecha_nacimiento: dt.date | None = None
    fecha_nacimiento_source: PoliticianSource | None = None
    origen: Literal["curado", "investigacion"]
    estado: Estado
    roles: list[Role] = Field(default_factory=list)
    parties: list[Membership] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self):
        if self.estado == "automatico" and self.origen != "investigacion":
            raise ValueError("solo lo investigado puede ser automático")
        if self.fecha_nacimiento and self.fecha_nacimiento_source is None:
            raise ValueError("la fecha de nacimiento necesita fuente")
        return self

    def party_on(self, day: dt.date) -> Membership | None:
        """The party on that day; None if unknown or if the sources disagree."""
        found = [m for m in self.parties if m.holds_on(day)]
        names = {fold(m.partido) for m in found}
        if len(names) != 1:
            return None
        # Prefer a dated period over a snapshot.
        return min(found, key=lambda m: m.inicio is None)

    def roles_on(self, day: dt.date) -> list[Role]:
        return [r for r in self.roles if r.holds_on(day)]


def match(name: str, politicians: list[Politician]) -> Politician | None:
    """The only usable politician whose name or an alias contains every word of `name`
    ("Feijóo", "Sr. Núñez Feijóo"); None when nobody or more than one fits."""
    words = name_words(name)
    if not words:
        return None
    hits = []
    for p in politicians:
        if p.estado not in USABLE:
            continue
        for candidate in (p.nombre, *p.alias):
            have = set(name_words(candidate))
            if all(w in have for w in words):
                hits.append(p)
                break
    return hits[0] if len(hits) == 1 else None


class PoliticianDirectory(Protocol):
    def usable(self) -> list[Politician]:
        """Politicians that can be used (aprobado or automatico)."""
        ...

    def rejected_ids(self) -> set[str]:
        """Ids a person rejected: never used, never recorded again."""
        ...

    def record(self, politician: Politician) -> None:
        """Store an automatic research result (never overrides curated or rejected ones)."""
        ...


class LocalPoliticianCache:
    """data/cache/politicians.json: automatic research results, one entry per id."""

    def __init__(self, path: Path = DEFAULT_CACHE) -> None:
        self.path = path

    def _load(self) -> dict[str, Politician]:
        if not self.path.is_file():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {k: Politician.model_validate(v) for k, v in raw.items()}

    def _save(self, data: dict[str, Politician]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {k: v.model_dump(mode="json") for k, v in sorted(data.items())}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def usable(self) -> list[Politician]:
        return [p for p in self._load().values() if p.estado in USABLE]

    def rejected_ids(self) -> set[str]:
        return {i for i, p in self._load().items() if p.estado == "rechazado"}

    def record(self, politician: Politician) -> None:
        data = self._load()
        current = data.get(politician.id)
        if current is not None:
            if current.origen != "investigacion" or current.estado == "rechazado":
                return
            politician = merge_research(current, politician)
        data[politician.id] = politician
        self._save(data)

    def forget(self, ids: set[str]) -> None:
        """Drop entries rejected elsewhere (the database is the reference)."""
        data = self._load()
        if ids & data.keys():
            self._save({k: v for k, v in data.items() if k not in ids})


def merge_research(current: Politician, new: Politician) -> Politician:
    """Add the new sightings of an automatic entry (aliases, roles, parties)."""
    aliases = list(dict.fromkeys([*current.alias, *new.alias]))
    roles = current.roles + [r for r in new.roles if r not in current.roles]
    parties = current.parties + [m for m in new.parties if m not in current.parties]
    return current.model_copy(update={"alias": aliases, "roles": roles, "parties": parties})


class CombinedDirectory:
    """The database when configured (shared, with reviews) plus the local cache.

    The database decides: an entry rejected there is not used even if the cache has it.
    A database failure is logged and reported through `errors`, never hidden."""

    def __init__(self, cache: LocalPoliticianCache, database: PoliticianDirectory | None) -> None:
        self.cache = cache
        self.database = database
        self.errors: list[str] = []
        self._rejected: set[str] = set()

    def usable(self) -> list[Politician]:
        found: dict[str, Politician] = {}
        rejected: set[str] = set()
        if self.database is not None:
            try:
                for p in self.database.usable():
                    found[p.id] = p
                rejected = self.database.rejected_ids()
            except Exception as exc:  # noqa: BLE001 - reported, the cache still works
                logger.warning("Base de políticos no disponible: %s", exc)
                self.errors.append(f"Base de políticos no disponible: {exc}")
        self._rejected = rejected | self.cache.rejected_ids()
        if rejected:
            self.cache.forget(rejected)
        for p in self.cache.usable():
            found.setdefault(p.id, p)
        return list(found.values())

    def rejected_ids(self) -> set[str]:
        return set(self._rejected)

    def record(self, politician: Politician) -> None:
        if politician.id in self._rejected:
            return
        self.cache.record(politician)
        if self.database is not None:
            try:
                self.database.record(politician)
            except Exception as exc:  # noqa: BLE001 - reported, kept in the cache
                logger.warning("No se pudo guardar %s en la base: %s", politician.id, exc)
                self.errors.append(f"No se pudo guardar {politician.nombre} en la base: {exc}")
