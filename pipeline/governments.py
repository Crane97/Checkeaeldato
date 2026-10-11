"""Base de conocimiento de gobiernos y lógica de fechas determinista.

Responde con código, nunca con el LLM: qué Gobierno había en una fecha, cuál fue el
anterior, en qué periodos gobernó un partido (incluido como socio de coalición) o un
presidente. Los datos están en data/knowledge/governments.json, versionado y actualizado a
mano. El esquema admite niveles autonómico y municipal; por ahora solo se usa el nacional.
"""

import datetime as dt
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, ValidationError, model_validator

from pipeline.models import DateRange

DEFAULT_KNOWLEDGE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "knowledge" / "governments.json"
)

Level = Literal["nacional", "autonomico", "municipal"]


class KnowledgeError(RuntimeError):
    """The knowledge base is missing or inconsistent."""


class OpenRange(BaseModel):
    start: dt.date
    end: dt.date | None = None


class CoalitionPartner(OpenRange):
    party: str


class GovernmentTerm(BaseModel):
    """One president's continuous time in office (several legislatures)."""

    id: str
    level: Level
    territory: str
    president: str
    parties: list[str]
    start: dt.date
    end: dt.date | None = None  # None: still in office at `updated`
    coalition_partners: list[CoalitionPartner] = Field(default_factory=list)
    caretaker: list[OpenRange] = Field(default_factory=list)
    appointment: str | None = None
    notes: str | None = None


class Party(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)


class Knowledge(BaseModel):
    version: int
    updated: dt.date
    sources: list[str] = Field(default_factory=list)
    notes: str | None = None
    parties: dict[str, Party] = Field(default_factory=dict)
    governments: list[GovernmentTerm]

    @model_validator(mode="after")
    def _no_overlaps(self) -> Self:
        by_place: dict[tuple[str, str], list[GovernmentTerm]] = {}
        for term in self.governments:
            by_place.setdefault((term.level, term.territory), []).append(term)
        for terms in by_place.values():
            terms.sort(key=lambda t: t.start)
            for prev, nxt in zip(terms, terms[1:], strict=False):
                if prev.end is None or prev.end > nxt.start:
                    raise ValueError(f"Los mandatos {prev.id} y {nxt.id} se solapan")
            for term in terms:
                if term.end is not None and term.end <= term.start:
                    raise ValueError(f"El mandato {term.id} termina antes de empezar")
        return self


def load_knowledge(path: Path = DEFAULT_KNOWLEDGE_PATH) -> Knowledge:
    try:
        return Knowledge.model_validate(json.loads(path.read_text()))
    except FileNotFoundError as exc:
        raise KnowledgeError(f"No existe la tabla de gobiernos {path}") from exc
    except (ValidationError, ValueError) as exc:
        raise KnowledgeError(f"Tabla de gobiernos inválida ({path.name}): {exc}") from exc


@lru_cache
def default_governments() -> "Governments":
    return Governments(load_knowledge())


def _fold(text: str) -> str:
    """Casefold and strip accents, for tolerant name matching."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


_NAME_NOISE = {
    "senor",
    "senora",
    "don",
    "dona",
    "presidente",
    "presidenta",
    "el",
    "la",
    "los",
    "las",
}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", _fold(text))) - _NAME_NOISE


class Governments:
    """Date logic over one level and territory of the knowledge base."""

    def __init__(
        self, knowledge: Knowledge, level: Level = "nacional", territory: str = "España"
    ) -> None:
        self.knowledge = knowledge
        self.terms = sorted(
            (t for t in knowledge.governments if t.level == level and t.territory == territory),
            key=lambda t: t.start,
        )

    # --- single dates -------------------------------------------------------

    def at(self, day: dt.date) -> GovernmentTerm | None:
        """Government in office on `day` (the handover day belongs to the newcomer)."""
        for term in self.terms:
            if term.start <= day and (term.end is None or day < term.end):
                return term
        return None

    def previous(self, day: dt.date) -> GovernmentTerm | None:
        current = self.at(day)
        if current is None:
            return None
        index = self.terms.index(current)
        return self.terms[index - 1] if index > 0 else None

    def partners_at(self, day: dt.date) -> list[str]:
        term = self.at(day)
        if term is None:
            return []
        return [
            p.party
            for p in term.coalition_partners
            if p.start <= day and (p.end is None or day < p.end)
        ]

    def in_caretaker(self, term: GovernmentTerm, day: dt.date) -> bool:
        return any(c.start <= day and (c.end is None or day < c.end) for c in term.caretaker)

    def describe(self, term: GovernmentTerm, day: dt.date) -> str:
        details = ", ".join(term.parties)
        partners = self.partners_at(day) if self.at(day) is term else []
        if partners:
            details += f", en coalición con {' y '.join(partners)}"
        if self.in_caretaker(term, day):
            details += ", en funciones"
        return f"Gobierno de {term.president} ({details})"

    # --- periods ------------------------------------------------------------

    @staticmethod
    def year_range(year: int) -> DateRange:
        return DateRange(start=dt.date(year, 1, 1), end=dt.date(year, 12, 31))

    @staticmethod
    def _merge(ranges: list[DateRange]) -> list[DateRange]:
        merged: list[DateRange] = []
        for r in sorted(ranges, key=lambda r: r.start):
            if merged and r.start <= merged[-1].end:
                merged[-1] = DateRange(start=merged[-1].start, end=max(merged[-1].end, r.end))
            else:
                merged.append(r)
        return merged

    def party_periods(self, party: str, until: dt.date) -> list[DateRange]:
        """Periods up to `until` in which `party` governed, leading or as coalition partner."""
        ranges: list[DateRange] = []
        for term in self.terms:
            if term.start >= until:
                continue
            term_end = min(term.end or until, until)
            if party in term.parties:
                ranges.append(DateRange(start=term.start, end=term_end))
            for partner in term.coalition_partners:
                if partner.party != party or partner.start >= until:
                    continue
                end = min(partner.end or until, term_end)
                ranges.append(DateRange(start=max(partner.start, term.start), end=end))
        return self._merge(ranges)

    def president_terms(self, name: str) -> list[GovernmentTerm]:
        """Terms of the president whose name contains all the words of `name`
        (titles like "señor" are ignored, so "señor Zapatero" works)."""
        wanted = _tokens(name)
        return [t for t in self.terms if wanted and wanted <= _tokens(t.president)]

    def president_periods(self, name: str, until: dt.date) -> list[DateRange]:
        return self._merge(
            [
                DateRange(start=t.start, end=min(t.end or until, until))
                for t in self.president_terms(name)
                if t.start < until
            ]
        )

    def current_party_tenure(self, day: dt.date) -> list[DateRange]:
        """The continuous period, up to `day`, of the party leading the government on `day`."""
        term = self.at(day)
        if term is None or not term.parties:
            return []
        for r in self.party_periods(term.parties[0], until=day):
            if r.start <= day <= r.end:
                return [r]
        return []

    # --- parties ------------------------------------------------------------

    def canonical_party(self, text: str) -> str | None:
        """Party id for a name or alias in the knowledge base ("Partido Popular" -> "PP")."""
        folded = _fold(text).strip()
        # "Partido Popular (PP)": try the whole text, then each part.
        candidates = [folded, *(p.strip() for p in re.split(r"[()]", folded) if p.strip())]
        for candidate in candidates:
            candidate = re.sub(r"^(el|la|los|las)\s+", "", candidate)
            for party_id, party in self.knowledge.parties.items():
                names = {party_id, party.name, *party.aliases}
                if candidate in {_fold(n) for n in names}:
                    return party_id
        return None
