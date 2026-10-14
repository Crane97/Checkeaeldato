"""Carga de data/knowledge/governments.json en ref.governments y ref.parties.

Los gobiernos se curan a mano: entran como `pendiente` y una persona los aprueba con
`uv run conocimiento revisar`. La carga es idempotente: lo que ya está igual no se toca, y
si un gobierno o partido ya cargado cambió en el JSON se avisa en lugar de sobrescribirlo
(un dato ya revisado no se pisa en silencio).
"""

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from pipeline.governments import DEFAULT_KNOWLEDGE_PATH


@dataclass
class LoadReport:
    new: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)  # differs from the DB: not overwritten


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def load_governments(conn: psycopg.Connection, path: Path = DEFAULT_KNOWLEDGE_PATH) -> LoadReport:
    data = json.loads(path.read_text())
    report = LoadReport()

    for party_id, party in data["parties"].items():
        wanted = (party["name"], sorted(party.get("aliases", [])))
        row = conn.execute(
            "select nombre, aliases from ref.parties where id = %s", (party_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "insert into ref.parties (id, nombre, aliases) values (%s, %s, %s)",
                (party_id, *wanted),
            )
            report.new.append(f"partido {party_id}")
        elif (row[0], sorted(row[1])) == wanted:
            report.unchanged.append(f"partido {party_id}")
        else:
            report.changed.append(f"partido {party_id}")

    for g in data["governments"]:
        wanted = (
            g["level"],
            g["territory"],
            g["president"],
            list(g["parties"]),
            _date(g["start"]),
            _date(g.get("end")),
            g.get("appointment"),
            g.get("notes"),
        )
        partners = sorted(
            (p["party"], _date(p["start"]), _date(p.get("end")))
            for p in g.get("coalition_partners", [])
        )
        caretaker = sorted((_date(c["start"]), _date(c.get("end"))) for c in g.get("caretaker", []))
        row = conn.execute(
            "select nivel, territorio, presidente, partidos, inicio, fin, nombramiento, notas"
            " from ref.governments where id = %s",
            (g["id"],),
        ).fetchone()
        if row is None:
            conn.execute(
                "insert into ref.governments (id, nivel, territorio, presidente, partidos,"
                " inicio, fin, nombramiento, notas) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (g["id"], *wanted),
            )
            for party, start, end in partners:
                conn.execute(
                    "insert into ref.government_partners (government_id, partido, inicio, fin)"
                    " values (%s, %s, %s, %s)",
                    (g["id"], party, start, end),
                )
            for start, end in caretaker:
                conn.execute(
                    "insert into ref.government_caretaker (government_id, inicio, fin)"
                    " values (%s, %s, %s)",
                    (g["id"], start, end),
                )
            report.new.append(f"gobierno {g['id']}")
            continue
        stored_partners = sorted(
            conn.execute(
                "select partido, inicio, fin from ref.government_partners where government_id = %s",
                (g["id"],),
            ).fetchall()
        )
        stored_caretaker = sorted(
            conn.execute(
                "select inicio, fin from ref.government_caretaker where government_id = %s",
                (g["id"],),
            ).fetchall()
        )
        if tuple(row) == wanted and stored_partners == partners and stored_caretaker == caretaker:
            report.unchanged.append(f"gobierno {g['id']}")
        else:
            report.changed.append(f"gobierno {g['id']}")
    return report
