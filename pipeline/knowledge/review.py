"""Revisión humana de lo pendiente: `uv run conocimiento revisar`.

Lo curado a mano (SMI, pensiones, gobiernos, partidos, hechos) y lo que la validación dejó
en cuarentena no se usa para verificar hasta que una persona lo aprueba. Cada elemento se
muestra con su fuente, los motivos de la cuarentena y los valores con los que compararlo
(versión anterior del mismo dato y periodo anterior). La decisión queda registrada con el
nombre del revisor: en ref.observation_events (observaciones) o en revisado_por y
revisado_en (gobiernos, partidos y hechos).
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal

import psycopg

Kind = Literal["observacion", "gobierno", "partido", "hecho", "politico"]
Outcome = Literal["aprobado", "rechazado"]
KINDS: tuple[Kind, ...] = ("observacion", "gobierno", "partido", "hecho", "politico")


class ReviewError(Exception):
    """The item does not exist, was already reviewed, or the reviewer is missing."""


@dataclass
class ReviewItem:
    kind: Kind
    id: str
    title: str
    group: str  # items that can be approved together (same indicator, same table)
    lines: list[str] = field(default_factory=list)


def _num(value: object) -> str:
    return f"{float(value):g}" if value is not None else "—"  # type: ignore[arg-type]


def _observations(conn: psycopg.Connection, indicator: str | None) -> list[ReviewItem]:
    rows = conn.execute(
        "select o.id, o.indicator_id, i.nombre, o.periodo, o.territorio_code, o.valor, o.unidad,"
        " o.estado, o.version, o.fecha_publicacion, o.fecha_publicacion_estimada, o.raw,"
        " s.organismo, s.titulo, s.url, s.fecha,"
        " (select p.valor from ref.observations p where p.indicator_id = o.indicator_id"
        "   and p.periodo = o.periodo and p.territorio_code = o.territorio_code"
        "   and p.version = o.version - 1),"
        " prev.periodo, prev.valor"
        " from ref.observations o"
        " join ref.indicators i on i.id = o.indicator_id"
        " join ref.sources s on s.id = o.source_id"
        " left join lateral (select p.periodo, p.valor from ref.observations p"
        "   where p.indicator_id = o.indicator_id and p.territorio_code = o.territorio_code"
        "   and p.vigente and p.periodo_inicio < o.periodo_inicio"
        "   order by p.periodo_inicio desc limit 1) prev on true"
        " where o.estado in ('pendiente', 'cuarentena') and o.vigente"
        " and (%s::text is null or o.indicator_id = %s)"
        " order by o.indicator_id, o.territorio_code, o.periodo_inicio",
        (indicator, indicator),
    ).fetchall()
    items = []
    for (
        oid, ind, name, periodo, terr, valor, unidad, estado, version, pub, estimated, raw,
        org, title, url, src_date, before, prev_periodo, prev_valor,
    ) in rows:  # fmt: skip
        lines = [
            f"Valor: {_num(valor)} {unidad} · estado: {estado} · versión {version}",
            f"Fuente: {org} — {title}" + (f" ({src_date:%Y-%m-%d})" if src_date else ""),
            f"URL: {url}",
            f"Publicado: {pub:%Y-%m-%d}"
            + (" (fecha estimada por calendario)" if estimated else ""),
        ]
        if before is not None:
            lines.append(f"Versión anterior: {_num(before)} → {_num(valor)}")
        if prev_valor is not None:
            diff = float(valor) - float(prev_valor)
            lines.append(
                f"periodo anterior ({prev_periodo}): {_num(prev_valor)} (cambio {diff:+g})"
            )
        for motive in (raw or {}).get("motivos", []):
            lines.append(f"Motivo de cuarentena: {motive}")
        items.append(
            ReviewItem(
                kind="observacion",
                id=str(oid),
                title=f"{name} · {periodo} · {terr}",
                group=f"observacion:{ind}",
                lines=lines,
            )
        )
    return items


def _governments(conn: psycopg.Connection) -> list[ReviewItem]:
    items = []
    for gid, nivel, terr, pres, partidos, inicio, fin, nombramiento, notas in conn.execute(
        "select id, nivel, territorio, presidente, partidos, inicio, fin, nombramiento, notas"
        " from ref.governments where estado = 'pendiente' order by inicio"
    ).fetchall():
        lines = [
            f"{nivel} · {terr} · partidos: {', '.join(partidos)}",
            f"Desde {inicio:%Y-%m-%d} hasta {fin:%Y-%m-%d}" if fin else f"Desde {inicio:%Y-%m-%d}",
        ]
        if nombramiento:
            lines.append(f"Nombramiento: {nombramiento}")
        for partido, p_ini, p_fin in conn.execute(
            "select partido, inicio, fin from ref.government_partners where government_id = %s"
            " order by inicio",
            (gid,),
        ).fetchall():
            hasta = f"{p_fin:%Y-%m-%d}" if p_fin else "hoy"
            lines.append(f"Socio: {partido} ({p_ini:%Y-%m-%d} – {hasta})")
        for c_ini, c_fin in conn.execute(
            "select inicio, fin from ref.government_caretaker where government_id = %s"
            " order by inicio",
            (gid,),
        ).fetchall():
            hasta = f"{c_fin:%Y-%m-%d}" if c_fin else "hoy"
            lines.append(f"En funciones: {c_ini:%Y-%m-%d} – {hasta}")
        if notas:
            lines.append(f"Notas: {notas}")
        items.append(ReviewItem("gobierno", gid, f"Gobierno de {pres}", "gobierno", lines))
    return items


def _parties(conn: psycopg.Connection) -> list[ReviewItem]:
    return [
        ReviewItem("partido", pid, nombre, "partido", [f"Alias: {', '.join(aliases) or '—'}"])
        for pid, nombre, aliases in conn.execute(
            "select id, nombre, aliases from ref.parties where estado = 'pendiente' order by id"
        ).fetchall()
    ]


def _facts(conn: psycopg.Connection) -> list[ReviewItem]:
    return [
        ReviewItem(
            "hecho",
            str(fid),
            enunciado,
            "hecho",
            [
                f"Tema: {tema}" + (f" · fecha: {fecha:%Y-%m-%d}" if fecha else ""),
                f"Fuente: {org} — {title}",
                f"URL: {url}",
            ],
        )
        for fid, tema, enunciado, fecha, org, title, url in conn.execute(
            "select f.id, f.tema, f.enunciado, f.fecha, s.organismo, s.titulo, s.url"
            " from ref.facts f join ref.sources s on s.id = f.source_id"
            " where f.estado = 'pendiente' order by f.id"
        ).fetchall()
    ]


def _dates(inicio, fin, seen) -> str:
    if inicio is None:
        return f"constatado el {seen:%Y-%m-%d}"
    return f"{inicio:%Y-%m-%d} – {fin:%Y-%m-%d}" if fin else f"desde {inicio:%Y-%m-%d}"


def _politicians(conn: psycopg.Connection) -> list[ReviewItem]:
    from pipeline.knowledge.politicians_repo import read_politicians

    items = []
    people = read_politicians(conn, frozenset({"pendiente", "automatico"}))
    for p in sorted(people, key=lambda p: (p.estado != "automatico", p.nombre)):
        origin = "investigación automática (en uso)" if p.estado == "automatico" else "curado"
        lines = [f"Origen: {origin} · alias: {', '.join(p.alias) or '—'}"]
        if p.fecha_nacimiento and p.fecha_nacimiento_source:
            s = p.fecha_nacimiento_source
            lines.append(
                f"Nacimiento: {p.fecha_nacimiento:%Y-%m-%d} ({s.organismo}, {s.tipo}: {s.url})"
            )
        for m in p.parties:
            dates = _dates(m.inicio, m.fin, m.constatado_en)
            lines.append(f"Partido: {m.partido} ({dates}) · {m.source.organismo}, {m.source.tipo}")
            lines.append(f"   {m.source.url}")
        for r in p.roles:
            where = f", {r.organo}" if r.organo else ""
            caretaker = " [en funciones]" if r.en_funciones else ""
            dates = _dates(r.inicio, r.fin, r.constatado_en)
            lines.append(f"Cargo: {r.cargo}{where}{caretaker} ({dates}) · {r.source.organismo}")
            lines.append(f"   {r.source.url}")
        items.append(ReviewItem("politico", p.id, p.nombre, f"politico:{p.estado}", lines))
    return items


def pending_items(
    conn: psycopg.Connection,
    kinds: Collection[Kind] = KINDS,
    indicator: str | None = None,
) -> list[ReviewItem]:
    """Everything waiting for a person, in a stable order."""
    items: list[ReviewItem] = []
    if "observacion" in kinds:
        items += _observations(conn, indicator)
    if indicator is None:
        if "gobierno" in kinds:
            items += _governments(conn)
        if "partido" in kinds:
            items += _parties(conn)
        if "hecho" in kinds:
            items += _facts(conn)
        if "politico" in kinds:
            items += _politicians(conn)
    return items


_TABLES = {
    "gobierno": "ref.governments",
    "partido": "ref.parties",
    "hecho": "ref.facts",
    "politico": "ref.politicians",
}


def decide(
    conn: psycopg.Connection, kind: Kind, item_id: str, outcome: Outcome, *, reviewer: str
) -> None:
    """Approve or reject one item, in the caller's transaction."""
    reviewer = reviewer.strip()
    if not reviewer:
        raise ReviewError("Falta el nombre del revisor")
    if outcome not in ("aprobado", "rechazado"):
        raise ReviewError(f"Decisión no válida: {outcome}")
    if kind == "observacion":
        if not item_id.isdigit():
            raise ReviewError(f"Id de observación no válido: {item_id}")
        conn.execute("select set_config('app.revisor', %s, true)", (reviewer,))
        cur = conn.execute(
            "update ref.observations set estado = %s"
            " where id = %s and estado in ('pendiente', 'cuarentena')",
            (outcome, int(item_id)),
        )
    elif kind in _TABLES:
        id_value: object = int(item_id) if kind == "hecho" and item_id.isdigit() else item_id
        cur = conn.execute(
            f"update {_TABLES[kind]} set estado = %s, revisado_por = %s, revisado_en = now()"
            " where id = %s and estado in ('pendiente', 'automatico')",
            (outcome, reviewer, id_value),
        )
    else:
        raise ReviewError(f"Tipo no válido: {kind}")
    if cur.rowcount != 1:
        raise ReviewError(f"{kind} {item_id} no existe o ya no está pendiente")


def withdraw(
    conn: psycopg.Connection, kind: Kind, item_id: str, *, reviewer: str
) -> tuple[str, str | None]:
    """Reject something already approved (it stops being used at once). Returns the
    previous state and reviewer. Observations are immutable: a wrong value is corrected
    with a new version, not withdrawn."""
    reviewer = reviewer.strip()
    if not reviewer:
        raise ReviewError("Falta el nombre del revisor")
    if kind not in _TABLES:
        raise ReviewError("Las observaciones no se retiran: se corrigen con una versión nueva")
    table = _TABLES[kind]
    id_value: object = int(item_id) if kind == "hecho" and item_id.isdigit() else item_id
    row = conn.execute(
        f"select estado, revisado_por from {table} where id = %s for update", (id_value,)
    ).fetchone()
    if row is None or row[0] != "aprobado":
        raise ReviewError(f"{kind} {item_id} no existe o no está aprobado")
    conn.execute(
        f"update {table} set estado = 'rechazado', revisado_por = %s, revisado_en = now()"
        " where id = %s",
        (reviewer, id_value),
    )
    return row[0], row[1]
