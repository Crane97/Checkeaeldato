"""Recopilación de la base de políticos a partir de una semilla por cargo.

`data/knowledge/politicians_seed.yaml` dice QUIÉN entra (elegido por el cargo que ocupa,
nunca por el partido) y qué cargo actual se comprueba. De cada persona se recopila:
- cargos del BOE (fuente primaria): nombramientos y ceses con fecha; el fin de un cargo es
  su cese o el siguiente nombramiento para el mismo cargo. Los tramos del Gobierno de España
  en funciones salen de la tabla de gobiernos (data/knowledge/governments.json);
- nacimiento, partidos y cargos que el BOE no publica (diputado, alcalde, dirección de un
  partido) de Wikidata, marcados como fuente secundaria. Solo fechas con precisión de día;
- el cargo actual de la semilla: comprobado en el BOE (el último nombramiento para ese
  cargo debe ser de esta persona) o constatado hoy en la fuente oficial que se indique.
Todo sale como `curado` + `pendiente`: nada se usa hasta que una persona lo revisa.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from pipeline.governments import Governments
from pipeline.knowledge.collect_sources import BoeHit, WdFacts, dismissal
from pipeline.politicians import Membership, Politician, PoliticianSource, Role, fold, slug

logger = logging.getLogger(__name__)

SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "knowledge" / "politicians_seed.yaml"

# Offices whose holders the BOE publishes: Wikidata positions about them are left out to
# avoid duplicating the official rows with secondary ones.
_BOE_COVERED = (
    "ministr",
    "presidente del gobierno",
    "presidenta del gobierno",
    "vicepresident",
    "president de la generalitat",
    "presidente de la generalitat",
    "presidenta de la generalitat",
    "presidente de la junta",
    "presidenta de la junta",
    "presidente de la xunta",
    "presidente del principado",
    "presidente de la comunidad",
    "presidenta de la comunidad",
    "presidente de la region",
    "presidente de canarias",
    "presidente de aragon",
    "presidente de la ciudad",
    "presidenta de las illes",
    "presidente de las illes",
    "lehendakari",
    "presidente del gobierno vasco",
    "presidente de la rioja",
)
_REGIONS = {
    "andalucia": "ES-AN",
    "aragon": "ES-AR",
    "asturias": "ES-AS",
    "illes balears": "ES-IB",
    "canarias": "ES-CN",
    "cantabria": "ES-CB",
    "castilla y leon": "ES-CL",
    "castilla-la mancha": "ES-CM",
    "cataluna": "ES-CT",
    "generalitat valenciana": "ES-VC",
    "extremadura": "ES-EX",
    "galicia": "ES-GA",
    "comunidad de madrid": "ES-MD",
    "murcia": "ES-MC",
    "navarra": "ES-NC",
    "gobierno vasco": "ES-PV",
    "la rioja": "ES-RI",
    "ceuta": "ES-CE",
    "melilla": "ES-ML",
}


class SeedSource(BaseModel):
    organismo: str
    titulo: str
    url: str = Field(pattern=r"^https?://")


class SeedOffice(BaseModel):
    cargo: str
    organo: str | None = None
    territorio: str = "ES"
    # Either the exact BOE wording of the appointment ("Presidente de la Junta de Galicia")…
    boe: bool = False
    # …or the official page where it is seen today.
    fuente: SeedSource | None = None


class SeedPerson(BaseModel):
    nombre: str  # official full name, as in the BOE
    categoria: str
    alias: list[str] = Field(default_factory=list)
    wikidata: str | None = Field(default=None, pattern=r"^Q\d+$")
    cargos_actuales: list[SeedOffice] = Field(default_factory=list)


def load_seed(path: Path = SEED_PATH) -> list[SeedPerson]:
    people = [SeedPerson.model_validate(p) for p in yaml.safe_load(path.read_text("utf-8"))]
    ids = [slug(p.nombre) for p in people]
    if dupes := {i for i in ids if ids.count(i) > 1}:
        raise ValueError(f"Personas repetidas en la semilla: {', '.join(sorted(dupes))}")
    return people


def _boe_source(hit: BoeHit) -> PoliticianSource:
    return PoliticianSource(
        organismo="Boletín Oficial del Estado",
        titulo=hit.title[:300],
        url=hit.url,
        fecha=hit.published,
        tipo="primaria",
    )


def _organ(cargo: str) -> tuple[str | None, str]:
    folded = fold(cargo)
    if folded.startswith("ministr") or "del gobierno" in folded and "vasco" not in folded:
        return "Gobierno de España", "ES"
    for key, code in _REGIONS.items():
        if key in folded:
            return None, code
    return None, "ES"


def _split_caretaker(role: Role, governments: Governments) -> list[Role]:
    """Rows of a national government office, cut where the government was in caretaker."""
    if role.organo != "Gobierno de España" or role.inicio is None:
        return [role]
    cuts: list[tuple[dt.date, dt.date | None]] = []
    for term in governments.knowledge.governments:
        if term.level == "nacional":
            cuts += [(c.start, c.end) for c in term.caretaker]
    rows, cursor, end = [], role.inicio, role.fin
    for c_start, c_end in sorted(cuts):
        last = c_end - dt.timedelta(days=1) if c_end else None  # caretaker end is exclusive
        if (end is not None and c_start > end) or (last is not None and last < cursor):
            continue
        if c_start > cursor:
            rows.append(
                role.model_copy(update={"inicio": cursor, "fin": c_start - dt.timedelta(1)})
            )
        stop = min(x for x in (last, end) if x is not None) if (last or end) else None
        rows.append(
            role.model_copy(
                update={"inicio": max(cursor, c_start), "fin": stop, "en_funciones": True}
            )
        )
        if stop is None:
            return rows
        cursor = stop + dt.timedelta(days=1)
    if end is None or cursor <= end:
        rows.append(role.model_copy(update={"inicio": cursor, "fin": end}))
    return rows


def roles_from_boe(
    nombre: str,
    appointments: list[BoeHit],
    dismissals: list[BoeHit],
    successors: Callable[[str, dt.date], list[BoeHit]],
    governments: Governments,
) -> list[Role]:
    """Official offices of a person: start at the appointment decree, end at the dismissal
    or the next appointment to the same office (that day minus one)."""
    me = fold(nombre)
    roles = []
    for hit in sorted(appointments, key=lambda h: h.published):
        a = hit.appointment()
        if a is None or fold(a[1]) != me:
            continue
        cargo, start = a[0], hit.decree_date()
        ends = [
            d.decree_date()
            for d in dismissals
            if (x := dismissal(d)) and fold(x[1]) == me and fold(x[0]) == fold(cargo)
            and d.decree_date() >= start
        ]  # fmt: skip
        ends += [
            s.decree_date() - dt.timedelta(days=1)
            for s in successors(cargo, start)
            if s.ident != hit.ident and s.decree_date() > start
            and (x := s.appointment()) and fold(x[0]) == fold(cargo)
        ]  # fmt: skip
        organo, territorio = _organ(cargo)
        role = Role(
            cargo=cargo,
            organo=organo,
            territorio=territorio,
            inicio=start,
            fin=min(ends) if ends else None,
            source=_boe_source(hit),
        )
        roles += _split_caretaker(role, governments)
    return roles


def _wd_source(facts: WdFacts) -> PoliticianSource:
    return PoliticianSource(
        organismo="Wikidata",
        titulo=f"{facts.label} ({facts.qid})",
        url=facts.url,
        tipo="secundaria",
    )


def from_wikidata(facts: WdFacts, today: dt.date) -> tuple[list[Role], list[Membership]]:
    source = _wd_source(facts)
    roles = [
        Role(cargo=p.label, inicio=p.start, fin=p.end, source=source)
        for p in facts.positions
        if p.start is not None and not any(k in fold(p.label) for k in _BOE_COVERED)
        and (p.end is None or p.end >= p.start)
    ]  # fmt: skip
    dated = [
        p for p in facts.parties if p.start is not None and (p.end is None or p.end >= p.start)
    ]
    parties = [Membership(partido=p.label, inicio=p.start, fin=p.end, source=source) for p in dated]
    undated_open = [p for p in facts.parties if p.start is None and p.end is None]
    if not any(p.end is None for p in dated) and len(undated_open) == 1:
        # One party with no dates and nothing else current: seen in Wikidata today.
        parties.append(
            Membership(partido=undated_open[0].label, constatado_en=today, source=source)
        )
    return roles, parties


@dataclass
class Collected:
    politician: Politician
    warnings: list[str] = field(default_factory=list)


def build_politician(
    seed: SeedPerson,
    *,
    today: dt.date,
    boe_roles: list[Role],
    current_checks: dict[str, BoeHit | None],
    facts: WdFacts | None,
) -> Collected:
    """Assemble one curated entry; `current_checks` maps each BOE office of the seed to its
    latest appointment (whoever it names)."""
    warnings: list[str] = []
    roles = list(boe_roles)
    parties: list[Membership] = []
    birth = birth_source = None
    if facts is not None:
        wd_roles, parties = from_wikidata(facts, today)
        roles += wd_roles
        if facts.birth:
            birth, birth_source = facts.birth, _wd_source(facts)
    else:
        warnings.append("sin Wikidata: no hay nacimiento ni partido")

    for office in seed.cargos_actuales:
        if office.boe:
            hit = current_checks.get(office.cargo)
            appointed = hit.appointment() if hit else None
            if appointed is None:
                warnings.append(f"no se encontró en el BOE el nombramiento «{office.cargo}»")
            elif fold(appointed[1]) != fold(seed.nombre):
                warnings.append(f"el último nombramiento «{office.cargo}» es de {appointed[1]}")
            elif not any(r.cargo == office.cargo and r.fin is None for r in roles):
                warnings.append(f"«{office.cargo}» no figura como vigente en el BOE")
        elif office.fuente is not None:
            s = office.fuente
            roles.append(
                Role(
                    cargo=office.cargo,
                    organo=office.organo,
                    territorio=office.territorio,
                    constatado_en=today,
                    source=PoliticianSource(
                        organismo=s.organismo, titulo=s.titulo, url=s.url, fecha=today
                    ),
                )
            )
        else:
            warnings.append(f"«{office.cargo}» sin fuente")
    if not parties:
        warnings.append("sin partido con fecha")

    politician = Politician(
        id=slug(seed.nombre),
        nombre=seed.nombre,
        alias=seed.alias,
        fecha_nacimiento=birth,
        fecha_nacimiento_source=birth_source,
        origen="curado",
        estado="pendiente",
        roles=roles,
        parties=parties,
    )
    return Collected(politician, warnings)
