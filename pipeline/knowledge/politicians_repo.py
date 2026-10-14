"""Políticos en Supabase: lectura para el pipeline, registro de investigaciones y carga del
JSON curado (data/knowledge/politicians.json, entra pendiente de revisión)."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from pipeline.politicians import USABLE, Membership, Politician, PoliticianSource, Role

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "knowledge" / "politicians.json"


class SourceConflictError(ValueError):
    """The same source is stored with another type (primary vs secondary)."""


def _source_id(conn: psycopg.Connection, s: PoliticianSource) -> int:
    source_id, tipo = conn.execute(
        "insert into ref.sources (organismo, titulo, url, fecha, tipo) values (%s, %s, %s, %s, %s)"
        " on conflict (organismo, titulo, url) do update set organismo = excluded.organismo"
        " returning id, tipo",
        (s.organismo, s.titulo, s.url, s.fecha, s.tipo),
    ).fetchone()
    if tipo != s.tipo:
        raise SourceConflictError(
            f"La fuente {s.url} ya está guardada como {tipo}, no como {s.tipo}"
        )
    return source_id


def _source(row: tuple) -> PoliticianSource:
    organismo, titulo, url, fecha, tipo = row
    return PoliticianSource(organismo=organismo, titulo=titulo, url=url, fecha=fecha, tipo=tipo)


def read_politicians(
    conn: psycopg.Connection, estados: frozenset[str] = USABLE
) -> list[Politician]:
    people = {
        pid: Politician(
            id=pid,
            nombre=nombre,
            alias=list(alias),
            fecha_nacimiento=nac,
            fecha_nacimiento_source=_source(src) if src[0] else None,
            origen=origen,
            estado=estado,
        )
        for pid, nombre, alias, nac, origen, estado, *src in conn.execute(
            "select p.id, p.nombre, p.alias, p.fecha_nacimiento, p.origen, p.estado,"
            " s.organismo, s.titulo, s.url, s.fecha, s.tipo"
            " from ref.politicians p left join ref.sources s on s.id = p.fecha_nacimiento_source_id"
            " where p.estado = any(%s)",
            (sorted(estados),),
        ).fetchall()
    }
    ids = list(people)
    for pid, cargo, organo, terr, ini, fin, func, seen, *src in conn.execute(
        "select r.politician_id, r.cargo, r.organo, r.territorio, r.inicio, r.fin,"
        " r.en_funciones, r.constatado_en, s.organismo, s.titulo, s.url, s.fecha, s.tipo"
        " from ref.politician_roles r join ref.sources s on s.id = r.source_id"
        " where r.politician_id = any(%s) order by r.politician_id, r.inicio nulls last, r.id",
        (ids,),
    ).fetchall():
        people[pid].roles.append(
            Role(
                cargo=cargo,
                organo=organo,
                territorio=terr,
                inicio=ini,
                fin=fin,
                en_funciones=func,
                constatado_en=seen,
                source=_source(tuple(src)),
            )
        )
    for pid, partido, ini, fin, seen, *src in conn.execute(
        "select m.politician_id, m.partido, m.inicio, m.fin, m.constatado_en,"
        " s.organismo, s.titulo, s.url, s.fecha, s.tipo"
        " from ref.politician_parties m join ref.sources s on s.id = m.source_id"
        " where m.politician_id = any(%s) order by m.politician_id, m.inicio nulls last, m.id",
        (ids,),
    ).fetchall():
        people[pid].parties.append(
            Membership(
                partido=partido, inicio=ini, fin=fin, constatado_en=seen, source=_source(tuple(src))
            )
        )
    return list(people.values())


def _insert_rows(conn: psycopg.Connection, p: Politician) -> None:
    for r in p.roles:
        conn.execute(
            "insert into ref.politician_roles (politician_id, cargo, organo, territorio, inicio,"
            " fin, en_funciones, constatado_en, source_id)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                p.id,
                r.cargo,
                r.organo,
                r.territorio,
                r.inicio,
                r.fin,
                r.en_funciones,
                r.constatado_en,
                _source_id(conn, r.source),
            ),
        )
    for m in p.parties:
        conn.execute(
            "insert into ref.politician_parties (politician_id, partido, inicio, fin,"
            " constatado_en, source_id) values (%s, %s, %s, %s, %s, %s)",
            (p.id, m.partido, m.inicio, m.fin, m.constatado_en, _source_id(conn, m.source)),
        )


def _insert(conn: psycopg.Connection, p: Politician) -> None:
    birth_source = (
        _source_id(conn, p.fecha_nacimiento_source) if p.fecha_nacimiento_source else None
    )
    conn.execute(
        "insert into ref.politicians (id, nombre, alias, fecha_nacimiento,"
        " fecha_nacimiento_source_id, origen, estado) values (%s, %s, %s, %s, %s, %s, %s)",
        (p.id, p.nombre, p.alias, p.fecha_nacimiento, birth_source, p.origen, p.estado),
    )
    _insert_rows(conn, p)


# Namespace of the advisory locks taken while recording research (one per politician id).
_RESEARCH_LOCK = 727275


def record_research(conn: psycopg.Connection, p: Politician) -> str:
    """Store an automatic entry; returns what happened. Curated, approved or rejected
    entries are never touched: only automatic ones gain the new sightings."""
    # Two analyses may research the same new person at once: serialize per id, so the
    # second one sees the first one's row instead of failing on the primary key.
    conn.execute("select pg_advisory_xact_lock(%s, hashtext(%s))", (_RESEARCH_LOCK, p.id))
    row = conn.execute(
        "select origen, estado, alias from ref.politicians where id = %s for update", (p.id,)
    ).fetchone()
    if row is None:
        _insert(conn, p)
        return "nuevo"
    origen, estado, alias = row
    if origen != "investigacion" or estado != "automatico":
        return "sin_cambios"
    current = read_politicians(conn, frozenset({"automatico"}))
    existing = next(c for c in current if c.id == p.id)
    new_rows = p.model_copy(
        update={
            "roles": [r for r in p.roles if r not in existing.roles],
            "parties": [m for m in p.parties if m not in existing.parties],
        }
    )
    merged_alias = list(dict.fromkeys([*alias, *p.alias]))
    if merged_alias != list(alias):
        conn.execute("update ref.politicians set alias = %s where id = %s", (merged_alias, p.id))
    _insert_rows(conn, new_rows)
    return "actualizado"


class PostgresPoliticianDirectory:
    """The shared base for the pipeline; one short connection per call."""

    def __init__(self, connect: Callable[[], psycopg.Connection]) -> None:
        self._connect = connect
        self._rejected: set[str] | None = None

    def usable(self) -> list[Politician]:
        with self._connect() as conn:
            people = read_politicians(conn)
            self._rejected = {
                r[0]
                for r in conn.execute(
                    "select id from ref.politicians where estado = 'rechazado'"
                ).fetchall()
            }
            conn.rollback()
        return people

    def rejected_ids(self) -> set[str]:
        if self._rejected is None:
            self.usable()
        return set(self._rejected or ())

    def record(self, politician: Politician) -> None:
        with self._connect() as conn:
            record_research(conn, politician)
            conn.commit()


@dataclass
class LoadReport:
    new: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    # For each updated entry, the review it had before going back to pending (audit trail).
    previous_reviews: list[str] = field(default_factory=list)


def load_curated(path: Path = DEFAULT_PATH) -> list[Politician]:
    data = json.loads(path.read_text(encoding="utf-8"))
    people = [Politician.model_validate(p) for p in data["politicos"]]
    ids = [p.id for p in people]
    if dupes := {i for i in ids if ids.count(i) > 1}:
        raise ValueError(f"Ids duplicados en {path.name}: {', '.join(sorted(dupes))}")
    if wrong := [p.id for p in people if p.origen != "curado" or p.estado != "pendiente"]:
        raise ValueError(f"Lo curado entra como origen=curado, estado=pendiente: {wrong}")
    return people


def _comparable(p: Politician) -> dict:
    data = p.model_dump(mode="json", exclude={"estado"})
    data["roles"] = sorted(data["roles"], key=json.dumps)
    data["parties"] = sorted(data["parties"], key=json.dumps)
    return data


def load_politicians(
    conn: psycopg.Connection, path: Path = DEFAULT_PATH, *, update: bool = False
) -> LoadReport:
    """Insert new curated politicians as pending. A changed entry is reported; with
    `update` it is replaced and goes back to review (in the caller's transaction)."""
    report = LoadReport()
    stored = {
        p.id: p
        for p in read_politicians(
            conn, frozenset({"pendiente", "automatico", "aprobado", "rechazado"})
        )
    }
    for p in load_curated(path):
        current = stored.get(p.id)
        if current is None:
            _insert(conn, p)
            report.new.append(p.id)
        elif _comparable(current) == _comparable(p):
            report.unchanged.append(p.id)
        elif not update or current.origen != "curado":
            report.changed.append(p.id)
        else:
            prev = conn.execute(
                "select estado, revisado_por, revisado_en from ref.politicians where id = %s",
                (p.id,),
            ).fetchone()
            if prev[1]:
                report.previous_reviews.append(
                    f"{p.id}: {prev[0]} por {prev[1]} el {prev[2]:%Y-%m-%d}"
                )
            conn.execute("delete from ref.politician_roles where politician_id = %s", (p.id,))
            conn.execute("delete from ref.politician_parties where politician_id = %s", (p.id,))
            birth = (
                _source_id(conn, p.fecha_nacimiento_source) if p.fecha_nacimiento_source else None
            )
            conn.execute(
                "update ref.politicians set nombre = %s, alias = %s, fecha_nacimiento = %s,"
                " fecha_nacimiento_source_id = %s, estado = 'pendiente', revisado_por = null,"
                " revisado_en = null where id = %s",
                (p.nombre, p.alias, p.fecha_nacimiento, birth, p.id),
            )
            _insert_rows(conn, p)
            report.updated.append(p.id)
    return report
