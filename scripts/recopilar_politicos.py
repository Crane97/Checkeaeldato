"""Genera data/knowledge/politicians.json desde la semilla (data/knowledge/politicians_seed.yaml).

    uv run python scripts/recopilar_politicos.py            # todo
    uv run python scripts/recopilar_politicos.py --solo "Ana Gil Ruiz"

Consulta el buscador del BOE y la API de Wikidata (gratuitos, sin clave), con una pausa
entre peticiones y caché en data/cache/collect/ (no repite consultas al relanzar). Lo
generado entra como pendiente: revisa el diff y los avisos, cárgalo con
`uv run conocimiento cargar-politicos` y apruébalo con `uv run conocimiento revisar`.
"""

import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline.governments import default_governments  # noqa: E402
from pipeline.knowledge.collect_sources import (  # noqa: E402
    WIKIDATA_API,
    BoeHit,
    CongresoSeat,
    _get,
    boe_search,
    congreso_junta,
    dismissal,
    document_body,
    implied_dismissals,
    implied_in_body,
    latest_appointment,
    parse_collective,
    wd_entities,
    wd_facts,
)
from pipeline.knowledge.politicians_collect import (  # noqa: E402
    SEED_PATH,
    SeedPerson,
    build_politician,
    is_national_government,
    load_seed,
    roles_from_boe,
)
from pipeline.knowledge.politicians_repo import DEFAULT_PATH  # noqa: E402
from pipeline.politicians import fold  # noqa: E402

CACHE = REPO / "data" / "cache" / "collect"
PAUSE_SECONDS = 1.0
SPAIN, POLITICIAN = "Q29", "Q82955"


class Boe:
    """BOE title search with a disk cache (searches are keyed by their parameters)."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def search(self, terms: str, until: dt.date | None = None) -> list[BoeHit]:
        key = hashlib.sha256(f"{terms}|{until}".encode()).hexdigest()[:24]
        path = CACHE / "boe" / f"{key}.json"
        if path.is_file():
            rows = json.loads(path.read_text("utf-8"))
            return [BoeHit(r[0], dt.date.fromisoformat(r[1]), r[2]) for r in rows]
        time.sleep(PAUSE_SECONDS)
        hits = boe_search(self.client, terms, until=until)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [[h.ident, h.published.isoformat(), h.title] for h in hits]
        path.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
        return hits


COLLECTIVE_SEARCHES = (
    "nombran Ministros del Gobierno",
    "nombran Vicepresidentes del Gobierno",
    "nombran Vicepresidentas del Gobierno",
)
FIRST_YEAR = 1977  # first cabinet of the democracy


def body(client: httpx.Client, ident: str) -> str:
    path = CACHE / "boe_textos" / f"{ident}.html"
    if not path.is_file():
        time.sleep(PAUSE_SECONDS)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document_body(client, ident), "utf-8")
    return path.read_text("utf-8")


def collective_index(
    client: httpx.Client, boe: Boe
) -> tuple[list[BoeHit], list[dt.date], list[BoeHit]]:
    """Appointments made in collective decrees (whole cabinets), one per person; the dates
    of those decrees (each one replaces the whole cabinet); and the dismissals they imply
    ("conlleva el cese de don X como …")."""
    out: list[BoeHit] = []
    implied: list[BoeHit] = []
    dates: set[dt.date] = set()
    for terms in COLLECTIVE_SEARCHES:
        for hit in boe.search(terms):
            if hit.published.year < FIRST_YEAR or hit.title.startswith("Corrección"):
                continue
            text = body(client, hit.ident)
            appointed = parse_collective(hit, text)
            out += appointed
            implied += implied_in_body(hit, text)
            if appointed and "Ministros" in hit.title:
                dates.add(hit.decree_date())
    return out, sorted(dates), implied


def congreso_rows(client: httpx.Client) -> list:
    path = CACHE / "congreso" / "junta_portavoces_XV.json"
    if path.is_file():
        data = json.loads(path.read_text("utf-8"))
        return [
            CongresoSeat(
                **r
                | {
                    "alta": dt.date.fromisoformat(r["alta"]),
                    "baja": dt.date.fromisoformat(r["baja"]) if r["baja"] else None,
                }
            )
            for r in data
        ]
    rows = congreso_junta(client, "XV")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                r.__dict__
                | {"alta": r.alta.isoformat(), "baja": r.baja.isoformat() if r.baja else None}
                for r in rows
            ],
            ensure_ascii=False,
        ),
        "utf-8",
    )
    return rows


BROWSER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0"


def page_mentions(client: httpx.Client, url: str, person: SeedPerson) -> str | None:
    """A warning unless the official page names the person (a hint, not proof)."""
    path = CACHE / "paginas" / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".txt")
    if not path.is_file():
        try:
            response = client.get(url, headers={"User-Agent": BROWSER_AGENT})
        except httpx.HTTPError as exc:
            return f"no se pudo abrir {url} ({type(exc).__name__}): compruébalo a mano"
        if response.status_code != 200:
            return f"{url} respondió {response.status_code}: compruébalo a mano"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(response.text, "utf-8")
    text = fold(path.read_text("utf-8"))
    if any(fold(n) in text for n in [person.nombre, *person.alias]):
        return None
    return f"el nombre no aparece en {url}: compruébalo a mano"


def resolve_qid(client: httpx.Client, person: SeedPerson) -> tuple[str | None, str | None]:
    """The seed's QID, or the only Spanish politician found by name (else a warning)."""
    if person.wikidata:
        return person.wikidata, None
    time.sleep(PAUSE_SECONDS)
    params = {
        "action": "wbsearchentities",
        "search": person.nombre,
        "language": "es",
        "limit": 7,
        "format": "json",
    }
    found = [h["id"] for h in _get(client, WIKIDATA_API, params).json().get("search", [])]
    if not found:  # Wikidata labels often drop the second surname
        short = " ".join(person.alias[:1]) or " ".join(person.nombre.split()[:2])
        params["search"] = short
        found = [h["id"] for h in _get(client, WIKIDATA_API, params).json().get("search", [])]
    entities = wd_entities(client, found) if found else {}

    def values(e: dict, prop: str) -> set[str]:
        return {
            s["mainsnak"]["datavalue"]["value"]["id"]
            for s in e.get("claims", {}).get(prop, [])
            if s["mainsnak"].get("snaktype") == "value"
        }

    good = [
        q
        for q, e in entities.items()
        if SPAIN in values(e, "P27")
        and (POLITICIAN in values(e, "P106") or e.get("claims", {}).get("P39"))
    ]
    if len(good) == 1:
        return good[0], None
    return None, f"Wikidata: {len(good)} candidatos para {person.nombre} ({', '.join(good)})"


def main(
    solo: Annotated[
        str | None, typer.Option(help="Solo esta persona (nombre de la semilla)")
    ] = None,
    hoy: Annotated[str | None, typer.Option(help="Fecha de constatación AAAA-MM-DD")] = None,
) -> None:
    today = dt.date.fromisoformat(hoy) if hoy else dt.date.today()
    seed = load_seed(SEED_PATH)
    if solo:
        seed = [p for p in seed if fold(p.nombre) == fold(solo)]
    governments = default_governments()
    out, report = [], []
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        boe = Boe(client)
        collective, cabinet_changes, collective_ceses = collective_index(client, boe)
        junta = congreso_rows(client)
        qids = {}
        for person in seed:
            qid, warning = resolve_qid(client, person)
            qids[person.nombre] = qid
            if warning:
                report.append(f"{person.nombre}: {warning}")
        facts = wd_facts(client, sorted({q for q in qids.values() if q}))

        for n, person in enumerate(seed, 1):
            typer.echo(f"[{n}/{len(seed)}] {person.nombre}")
            mine = fold(person.nombre)
            appointments = boe.search(f"nombra {person.nombre}") + [
                h for h in collective if (a := h.appointment()) and fold(a[1]) == mine
            ]
            dismissals = boe.search(f"cese {person.nombre}") + [
                h for h in collective_ceses if (x := dismissal(h)) and fold(x[1]) == mine
            ]
            for h in boe.search(f"nombra {person.nombre}"):
                a = h.appointment()
                if a and fold(a[1]) == mine and is_national_government(a[0]):
                    dismissals += implied_dismissals(h, body(client, h.ident), person.nombre)

            def successors(
                cargo: str, start: dt.date, own: list[BoeHit] = appointments
            ) -> list[BoeHit]:
                same = fold(cargo)
                return (
                    boe.search(f"nombra {cargo}", until=start)
                    + own  # renewals of the same person
                    + [h for h in collective if (a := h.appointment()) and fold(a[0]) == same]
                )

            notes: list[str] = []
            roles = roles_from_boe(
                person.nombre,
                appointments,
                dismissals,
                successors,
                governments,
                cabinet_changes,
                notes=notes,
            )
            checks = {
                # Only the latest appointment matters here: first page (until = date.max).
                o.cargo: latest_appointment(successors(o.cargo, dt.date.max), o.cargo)
                for o in person.cargos_actuales
                if o.boe
            }
            q = qids[person.nombre]
            collected = build_politician(
                person,
                today=today,
                boe_roles=roles,
                current_checks=checks,
                facts=facts.get(q) if q else None,
                congreso=junta,
            )
            collected.warnings += notes
            for office in person.cargos_actuales:
                if office.fuente is not None and (
                    warning := page_mentions(client, office.fuente.url, person)
                ):
                    collected.warnings.append(f"«{office.cargo}»: {warning}")
            out.append((person, collected.politician))
            report += [f"{person.nombre}: {w}" for w in collected.warnings]

    if solo:
        for _, p in out:
            typer.echo(p.model_dump_json(indent=2))
        for line in report:
            typer.secho(f"Aviso: {line}", fg=typer.colors.YELLOW)
        return
    payload = {
        "version": 1,
        "generado": today.isoformat(),
        "criterio": (
            "Por cargo a fecha de generación, sin mirar el partido: Gobierno, presidencias "
            "del Congreso y del Senado, portavoces de los grupos, presidencias autonómicas, "
            "dirección nacional de los partidos con representación, expresidentes del "
            "Gobierno, miembros del Gobierno durante la legislatura y alcaldías de las cinco "
            "mayores ciudades (ver politicians_seed.yaml)."
        ),
        "politicos": [p.model_dump(mode="json") | {"categoria": s.categoria} for s, p in out],
    }
    DEFAULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    avisos = DEFAULT_PATH.with_name("politicians_avisos.txt")
    avisos.write_text("\n".join(report) + "\n", "utf-8")
    typer.echo(
        f"{len(out)} políticos en {DEFAULT_PATH.name}; {len(report)} avisos en {avisos.name}"
    )


if __name__ == "__main__":
    typer.run(main)
