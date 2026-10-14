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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from pipeline.governments import Governments
from pipeline.knowledge.collect_sources import (
    CONGRESO_JUNTA_URL,
    BoeHit,
    CongresoSeat,
    WdFacts,
    dismissal,
)
from pipeline.politicians import (
    Membership,
    Politician,
    PoliticianSource,
    Role,
    fold,
    name_words,
    slug,
)

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


def is_national_government(cargo: str) -> bool:
    """Ministers, vice-presidents and the president of the Government of Spain (not
    "Presidente del Gobierno de Canarias" or "del Gobierno Vasco")."""
    folded = fold(" ".join(cargo.split()))
    return folded.startswith("ministr") or folded.endswith("del gobierno")


def _organ(cargo: str) -> tuple[str | None, str]:
    folded = fold(cargo)
    if is_national_government(cargo):
        return "Gobierno de España", "ES"
    for key, code in _REGIONS.items():
        if key in folded:
            return None, code
    return None, "ES"


_CABINET_DELAY = dt.timedelta(days=30)
# A secondary office open for longer than this is probably a term Wikidata never closed.
_STALE_OPEN_TERM = dt.timedelta(days=4 * 365)


def _split_caretaker(role: Role, governments: Governments) -> list[Role]:
    """Rows of a national government office, cut where the government was in caretaker.

    The president is in caretaker for the periods of the governments table. Ministers stay
    in caretaker from the start of that period until their successor (or they themselves)
    is appointed, i.e. until the end of the row."""
    if role.organo != "Gobierno de España" or role.inicio is None:
        return [role]
    cuts: list[tuple[dt.date, dt.date | None]] = []
    president = fold(role.cargo) in ("presidente del gobierno", "presidenta del gobierno")
    for term in governments.knowledge.governments:
        if term.level != "nacional":
            continue
        for c in term.caretaker:
            if c.start <= role.inicio:
                continue  # an appointment ends the caretaker period: it never starts in it
            if president:
                cuts.append((c.start, c.end))
            elif role.inicio < c.start and (role.fin is None or c.start <= role.fin):
                # Safety cap: the new cabinet is appointed days after the investiture; a
                # missing decree must not stretch the caretaker period for years.
                ends = [x for x in (role.fin, c.end and c.end + _CABINET_DELAY) if x]
                cuts.append((c.start, min(ends) + dt.timedelta(days=1) if ends else None))
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
    cabinet_changes: Sequence[dt.date] = (),
) -> list[Role]:
    """Official offices of a person: start at the appointment decree; end at the dismissal
    (its date) or, without one, the day before the next appointment to the same office.
    A minister or vice-president also ends at the next collective cabinet decree
    (`cabinet_changes`): a new cabinet replaces every member, reappointed ones included."""
    me = fold(nombre)
    roles = []
    for hit in sorted(appointments, key=lambda h: h.published):
        a = hit.appointment()
        if a is None or fold(a[1]) != me:
            continue
        cargo, start = a[0], hit.decree_date()
        same = _norm(cargo)
        dismissed = [
            d.decree_date()
            for d in dismissals
            if (x := dismissal(d)) and fold(x[1]) == me and _norm(x[0]) == same
            and d.decree_date() >= start
        ]  # fmt: skip
        replaced = [
            s.decree_date()
            for s in successors(cargo, start)
            if s.ident != hit.ident and s.decree_date() > start
            and (x := s.appointment()) and _norm(x[0]) == same
        ]  # fmt: skip
        president = same in ("presidente del gobierno", "presidenta del gobierno")
        if is_national_government(cargo) and not president:
            replaced += [c for c in cabinet_changes if c > start]
        d, s = min(dismissed, default=None), min(replaced, default=None)
        if d is not None and (s is None or d <= s):
            end: dt.date | None = d
        else:
            end = s - dt.timedelta(days=1) if s is not None else None
        organo, territorio = _organ(cargo)
        role = Role(
            cargo=cargo,
            organo=organo,
            territorio=territorio,
            inicio=start,
            fin=end,
            source=_boe_source(hit),
        )
        roles += _split_caretaker(role, governments)
    return roles


def _norm(cargo: str) -> str:
    return fold(" ".join(cargo.split()))


# Congreso group acronyms (Junta de Portavoces) → group names.
_GROUPS = {
    "GS": "Grupo Parlamentario Socialista",
    "GP": "Grupo Parlamentario Popular en el Congreso",
    "GVOX": "Grupo Parlamentario VOX",
    "GSUMAR": "Grupo Parlamentario Plurinacional SUMAR",
    "GR": "Grupo Parlamentario Republicano",
    "GJXCAT": "Grupo Parlamentario Junts per Catalunya",
    "GEH BILDU": "Grupo Parlamentario Euskal Herria Bildu",
    "GV (EAJ-PNV)": "Grupo Parlamentario Vasco (EAJ-PNV)",
    "GMX": "Grupo Parlamentario Mixto",
}


def same_person(congreso_name: str, seed: "SeedPerson") -> bool:
    """ "López Álvarez, Patxi" is "Francisco Javier López Álvarez" (alias "Patxi López"):
    every surname in the official name and the given name in the name or an alias."""
    surnames, _, given = congreso_name.partition(",")
    official = set(name_words(seed.nombre))
    known = official | {w for a in seed.alias for w in name_words(a)}
    return all(w in official for w in name_words(surnames)) and any(
        w in known for w in name_words(given)
    )


def roles_from_congreso(seed: "SeedPerson", rows: list[CongresoSeat]) -> list[Role]:
    """Spokesperson terms of the Junta de Portavoces (official start and end dates)."""
    roles = []
    for r in rows:
        if r.cargo != "Portavoces titulares" or not same_person(r.nombre, seed):
            continue
        group = _GROUPS.get(r.siglas.upper(), r.siglas)
        roles.append(
            Role(
                cargo=f"Portavoz del {group}",
                organo="Congreso de los Diputados",
                inicio=r.alta,
                fin=r.baja,
                source=PoliticianSource(
                    organismo="Congreso de los Diputados",
                    titulo=f"Junta de Portavoces, {r.legislatura} Legislatura",
                    url=CONGRESO_JUNTA_URL,
                    tipo="primaria",
                ),
            )
        )
    return roles


# Dissolutions of the Cortes: every deputy's and senator's term ends that day.
DISSOLUTIONS = (dt.date(2026, 10, 6),)  # Real Decreto 806/2026 (BOE-A-2026-20742)


def _close_at_dissolution(role: Role) -> Role:
    folded = _norm(role.cargo)
    member = "diputad" in folded and ("congreso" in folded or "espana" in folded)
    if role.fin is not None or role.inicio is None or not (member or "senador" in folded):
        return role
    end = min((d for d in DISSOLUTIONS if d >= role.inicio), default=None)
    return role.model_copy(update={"fin": end}) if end else role


def _wd_source(facts: WdFacts) -> PoliticianSource:
    return PoliticianSource(
        organismo="Wikidata",
        titulo=f"{facts.label} ({facts.qid})",
        url=facts.url,
        tipo="secundaria",
    )


def _close_open_terms(roles: list[Role]) -> list[Role]:
    """Wikidata often leaves old terms open ("diputado" of two legislatures at once): an open
    term ends the day before the next term with the same name starts."""
    out = []
    by_name: dict[str, list[Role]] = {}
    for r in roles:
        by_name.setdefault(fold(r.cargo), []).append(r)
    for same in by_name.values():
        same.sort(key=lambda r: r.inicio or dt.date.min)
        for r, nxt in zip(same, [*same[1:], None], strict=True):
            if (
                r.fin is None
                and nxt is not None
                and nxt.inicio
                and r.inicio
                and nxt.inicio > r.inicio
            ):
                r = r.model_copy(update={"fin": nxt.inicio - dt.timedelta(days=1)})
            out.append(r)
    return out


def from_wikidata(facts: WdFacts, today: dt.date) -> tuple[list[Role], list[Membership]]:
    source = _wd_source(facts)
    roles = [
        Role(cargo=p.label, inicio=p.start, fin=p.end, source=source)
        for p in facts.positions
        if p.start is not None
        and not any(k in fold(p.label) for k in _BOE_COVERED)
        and (p.end is None or p.end >= p.start)
        and p.label != p.qid  # no label in Spanish or English: unreadable for a reviewer
    ]
    roles = _close_open_terms(roles)  # fmt: skip
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
    congreso: list[CongresoSeat] = (),  # type: ignore[assignment]
) -> Collected:
    """Assemble one curated entry; `current_checks` maps each BOE office of the seed to its
    latest appointment (whoever it names)."""
    warnings: list[str] = []
    roles = list(boe_roles) + roles_from_congreso(seed, list(congreso))
    parties: list[Membership] = []
    birth = birth_source = None
    if facts is not None:
        wd_roles, parties = from_wikidata(facts, today)
        official_now = {fold(o.cargo) for o in seed.cargos_actuales if o.fuente is not None}
        # The official page already says it holds today: drop the secondary open duplicate.
        roles += [r for r in wd_roles if not (r.fin is None and fold(r.cargo) in official_now)]
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
            elif not any(_norm(r.cargo) == _norm(office.cargo) and r.fin is None for r in roles):
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
    roles = [_close_at_dissolution(r) for r in roles]
    stale = today - _STALE_OPEN_TERM
    confirmed = {_norm(o.cargo) for o in seed.cargos_actuales}
    kept = []
    for r in roles:
        old_open = r.fin is None and r.inicio is not None and r.inicio < stale
        if old_open and _norm(r.cargo) not in confirmed:
            # The analysis would take it as current: left out, and listed for the reviewer.
            warnings.append(
                f"{r.source.organismo}: descartado «{r.cargo}», abierto desde {r.inicio}"
            )
        else:
            kept.append(r)
    roles = kept

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
