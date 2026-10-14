"""Fuentes para recopilar la base de políticos: buscador del BOE (primaria) y Wikidata
(secundaria). Solo las usa `scripts/recopilar_politicos.py`; nunca el análisis."""

import datetime as dt
import html
import re
from dataclasses import dataclass, field

import httpx

from pipeline.http import request_with_retries

# The BOE and the Congreso block generic clients; this identifies the project honestly.
USER_AGENT = "Mozilla/5.0 (compatible; CheckeaElDato/0.1; verificador de discursos)"
BOE_SEARCH_URL = "https://www.boe.es/buscar/boe.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
# Wikimedia's policy asks for a descriptive agent with a way to reach the project.
WIKIDATA_USER_AGENT = "CheckeaElDato/0.1 (https://github.com/Crane97/Checkeaeldato) python-httpx"

MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "enero",
            "febrero",
            "marzo",
            "abril",
            "mayo",
            "junio",
            "julio",
            "agosto",
            "septiembre",
            "octubre",
            "noviembre",
            "diciembre",
        ],
        1,
    )
}


class CollectError(RuntimeError):
    """A source could not be read."""


def _get(client: httpx.Client, url: str, params: list | dict) -> httpx.Response:
    agent = WIKIDATA_USER_AGENT if url == WIKIDATA_API else USER_AGENT
    response = request_with_retries(
        client,
        "GET",
        url,
        params=params,
        headers={"User-Agent": agent},
        max_attempts=5,
        backoff=10,
        error_cls=CollectError,
    )
    if response.status_code != 200:
        raise CollectError(f"HTTP {response.status_code} en {url}")
    return response


# --- BOE --------------------------------------------------------------------

_APPOINTMENT_RE = re.compile(
    r"por (?:el|la) que se nombra (?P<cargo>.+?) a (?:don|doña) (?P<nombre>[^.]+)\.$"
)
_ASSUMES_RE = re.compile(
    r"se dispone que (?:don|doña) (?P<nombre>[^,]+),[^.]*? asuma las funciones de"
    r" (?P<cargo>[^.]+)\.$"
)
_DECREE_DATE_RE = re.compile(r", de (\d{1,2}) de (\w+)(?: de (\d{4}))?,")


@dataclass(frozen=True)
class BoeHit:
    ident: str
    published: dt.date
    title: str

    @property
    def url(self) -> str:
        return f"https://www.boe.es/diario_boe/txt.php?id={self.ident}"

    def appointment(self) -> tuple[str, str] | None:
        """(cargo, nombre) if the title appoints someone, also "se dispone que doña X, …,
        asuma las funciones de Portavoz del Gobierno"."""
        m = _APPOINTMENT_RE.search(self.title) or _ASSUMES_RE.search(self.title)
        return (m.group("cargo"), m.group("nombre").strip()) if m else None

    def decree_date(self) -> dt.date:
        """Date of the decree ("Real Decreto 558/2026, de 3 de julio, …"); the publication
        date when the title does not give it."""
        m = _DECREE_DATE_RE.search(self.title)
        if not m or m.group(2).lower() not in MONTHS:
            return self.published
        year = int(m.group(3)) if m.group(3) else self.published.year
        day = dt.date(year, MONTHS[m.group(2).lower()], int(m.group(1)))
        return day if day <= self.published else day.replace(year=year - 1)


def parse_boe_results(page: str) -> list[BoeHit]:
    hits = []
    for block in re.findall(r'<li class="resultado-busqueda">(.*?)</li>', page, re.S):
        ident = re.search(r"BOE-A-\d{4}-\d+", block)
        date = re.search(r"de (\d{2})/(\d{2})/(\d{4})", block)
        title = re.search(r"<p>(.*?)</p>", block, re.S)
        if not (ident and date and title):
            continue
        d, m, y = (int(x) for x in date.groups())
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", title.group(1))).split())
        hits.append(BoeHit(ident.group(0), dt.date(y, m, d), text))
    return hits


_NEXT_PAGE_RE = re.compile(
    r'href="\.?/?boe\.php\?accion=Mas&(?:amp;)?id_busqueda=([^"]+?)-(\d+)-50"'
)
MAX_PAGES = 10


def boe_search(client: httpx.Client, terms: str, until: dt.date | None = None) -> list[BoeHit]:
    """Documents whose title has all `terms`, newest first: one page of 50, or, with
    `until`, more pages until one reaches documents published before that date. (The
    search matches words, so "nombra Presidente del Gobierno" also finds vice-presidents
    and regional presidents: 50 results may not go back far enough.)"""
    params = [
        ("campo[1]", "TITULOS"),
        ("dato[1]", terms),
        ("operador[1]", "and"),
        ("page_hits", "50"),
        ("sort_field[0]", "FPU"),
        ("sort_order[0]", "desc"),
        ("accion", "Buscar"),
    ]
    page = _get(client, BOE_SEARCH_URL, params).text
    hits = parse_boe_results(page)
    offset = 0
    while until is not None and hits and hits[-1].published >= until and offset < 50 * MAX_PAGES:
        links = {int(off): ident for ident, off in _NEXT_PAGE_RE.findall(page)}
        offset += 50
        if offset not in links:
            break
        page = _get(
            client, BOE_SEARCH_URL, {"accion": "Mas", "id_busqueda": f"{links[offset]}-{offset}-50"}
        ).text
        hits += parse_boe_results(page)
    return hits


_DISMISSAL_RE = re.compile(
    # "Se declara el cese … como Presidente del Gobierno" after an election only opens the
    # caretaker period: roles_from_boe decides with `declared` (see there).
    r"se (?:dispone|declara) el cese (?:de|como) (?:don|doña|D\.ª?|Dª) (?P<nombre>.+?),?"
    r" como (?P<cargo>[^.]+?)(?:,? a petición propia|, por [^.]+)?\.$"
)


def dismissal(hit: BoeHit) -> tuple[str, str] | None:
    """(cargo, nombre) if the title dismisses someone."""
    m = _DISMISSAL_RE.search(hit.title)
    return (m.group("cargo").strip(), m.group("nombre").strip()) if m else None


def declared(hit: BoeHit) -> bool:
    """ "Se declara el cese" (as opposed to "se dispone el cese")."""
    return "se declara el cese" in hit.title


def latest_appointment(hits: list[BoeHit], cargo: str) -> BoeHit | None:
    """The newest appointment to exactly this office (case-insensitive)."""
    wanted = " ".join(cargo.split()).casefold()
    found = [h for h in hits if (a := h.appointment()) is not None and a[0].casefold() == wanted]
    return max(found, key=lambda h: h.published, default=None)


# --- Wikidata ---------------------------------------------------------------


@dataclass(frozen=True)
class WdStatement:
    qid: str
    label: str
    start: dt.date | None
    end: dt.date | None
    # A reference other than "imported from Wikimedia project" / "retrieved".
    sourced: bool = False


_IMPORT_ONLY = {"P143", "P4656", "P813"}


def _sourced(statement: dict) -> bool:
    return any(set(ref.get("snaks", {})) - _IMPORT_ONLY for ref in statement.get("references", []))


@dataclass
class WdFacts:
    qid: str
    label: str
    birth: dt.date | None = None
    eswiki: str | None = None
    positions: list[WdStatement] = field(default_factory=list)
    parties: list[WdStatement] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.qid}"


def _wd_date(value: dict | None) -> dt.date | None:
    """Only day-precision dates (precision 11): "1972" alone is not a date."""
    if not value or value.get("precision") != 11:
        return None
    y, m, d = value["time"].lstrip("+").split("T")[0].split("-")
    return dt.date(int(y), int(m), int(d))


def _qualifier_date(statement: dict, prop: str) -> dt.date | None:
    for q in statement.get("qualifiers", {}).get(prop, []):
        if q.get("snaktype") == "value":
            return _wd_date(q["datavalue"]["value"])
    return None


def _item(statement: dict) -> str | None:
    snak = statement["mainsnak"]
    return snak["datavalue"]["value"]["id"] if snak.get("snaktype") == "value" else None


def wd_entities(client: httpx.Client, qids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(qids), 50):
        params = {
            "action": "wbgetentities",
            "ids": "|".join(qids[i : i + 50]),
            "props": "labels|claims|sitelinks",
            "languages": "es|en",
            "sitefilter": "eswiki",
            "format": "json",
        }
        out |= _get(client, WIKIDATA_API, params).json()["entities"]
    return out


def _label(entity: dict) -> str:
    labels = entity.get("labels", {})
    return (labels.get("es") or labels.get("en") or {}).get("value", entity.get("id", ""))


def parse_entity(entity: dict, labels: dict[str, str]) -> WdFacts:
    claims = entity.get("claims", {})
    facts = WdFacts(qid=entity["id"], label=_label(entity))
    for s in claims.get("P569", []):
        if s["mainsnak"].get("snaktype") == "value" and s.get("rank") != "deprecated":
            facts.birth = _wd_date(s["mainsnak"]["datavalue"]["value"])
            break
    if title := entity.get("sitelinks", {}).get("eswiki", {}).get("title"):
        facts.eswiki = "https://es.wikipedia.org/wiki/" + title.replace(" ", "_")
    for prop, target in (("P39", facts.positions), ("P102", facts.parties)):
        for s in claims.get(prop, []):
            qid = _item(s)
            if qid is None or s.get("rank") == "deprecated":
                continue
            target.append(
                WdStatement(
                    qid=qid,
                    label=labels.get(qid, qid),
                    start=_qualifier_date(s, "P580"),
                    end=_qualifier_date(s, "P582"),
                    sourced=_sourced(s),
                )
            )
    return facts


def wd_facts(client: httpx.Client, qids: list[str]) -> dict[str, WdFacts]:
    """People's facts with the labels of their positions and parties resolved."""
    people = wd_entities(client, qids)
    refs = sorted(
        {
            _item(s)
            for e in people.values()
            for prop in ("P39", "P102")
            for s in e.get("claims", {}).get(prop, [])
            if _item(s)
        }
    )
    labels = {q: _label(e) for q, e in wd_entities(client, refs).items()} if refs else {}
    return {q: parse_entity(e, labels) for q, e in people.items() if "missing" not in e}


_BODY_LINE_RE = re.compile(r"^(?P<cargo>[A-ZÁÉÍÓÚ][^.]*?) a (?P<art>don|doña) (?P<nombre>[^.]+)\.$")
_DECREE_PREFIX_RE = re.compile(r"^(.+?, de \d{1,2} de \w+(?: de \d{4})?),")


def parse_collective(hit: BoeHit, body: str) -> list[BoeHit]:
    """One appointment per line of a collective decree ("Real Decreto 835/2023, … por el
    que se nombran Ministros del Gobierno"), with the decree's id, date and number."""
    prefix = _DECREE_PREFIX_RE.match(hit.title)
    if prefix is None:
        return []
    text = html.unescape(re.sub(r"<[^>]+>", "\n", body))
    out = []
    for line in (x.strip() for x in text.split("\n")):
        m = _BODY_LINE_RE.match(line)
        if m:
            line = " ".join(line.split())  # the BOE uses non-breaking spaces ("Agenda 2030")
            m = _BODY_LINE_RE.match(line)
            assert m is not None
            title = (
                f"{prefix.group(1)}, por el que se nombra {m.group('cargo')} a"
                f" {m.group('art')} {m.group('nombre').strip()}."
            )
            out.append(BoeHit(hit.ident, hit.published, title))
    return out


def document_body(client: httpx.Client, ident: str) -> str:
    page = _get(client, "https://www.boe.es/diario_boe/txt.php", {"id": ident}).text
    m = re.search(r'<div id="textoxslt">(.*?)</div>', page, re.S)
    if m is None:
        raise CollectError(f"No se encontró el texto de {ident}")
    return m.group(1)


# --- "conlleva el cese" -------------------------------------------------------

_IMPLIED_RE = re.compile(
    r"conlleva(?:rá)? el cese(?: de (?:don|doña) [^,]+?)? como (?P<cargo>[^.;]+?)(?:[.;]|, )"
)


_IMPLIED_NAMED_RE = re.compile(
    r"conlleva(?:rá)? el cese de (?P<art>don|doña) (?P<nombre>[^,]+?)"
    r" como (?P<cargo>[^.;]+?)(?:[.;]|, )"
)


def implied_in_body(hit: BoeHit, body: str) -> list[BoeHit]:
    """Every "conlleva el cese de don X como Y" in a decree (also collective ones, e.g. the
    2020 cabinet decree ending Escrivá's presidency of the AIReF), dated as the decree."""
    prefix = _DECREE_PREFIX_RE.match(hit.title)
    if prefix is None:
        return []
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())
    return [
        BoeHit(
            hit.ident,
            hit.published,
            f"{prefix.group(1)}, por el que se dispone el cese de {m.group('art')}"
            f" {m.group('nombre').strip()} como {m.group('cargo').strip()}.",
        )
        for m in _IMPLIED_NAMED_RE.finditer(text)
    ]


def implied_dismissals(hit: BoeHit, body: str, nombre: str) -> list[BoeHit]:
    """An appointment decree may say it "conlleva el cese … como Secretario de Estado de
    …": one dismissal per office, dated as the appointment."""
    prefix = _DECREE_PREFIX_RE.match(hit.title)
    a = hit.appointment()
    if prefix is None or a is None:
        return []
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())
    art = "doña" if " a doña " in hit.title else "don"
    return [
        BoeHit(
            hit.ident,
            hit.published,
            f"{prefix.group(1)}, por el que se dispone el cese de {art} {nombre}"
            f" como {m.group('cargo').strip()}.",
        )
        for m in _IMPLIED_RE.finditer(text)
    ]


# --- Congreso: Junta de Portavoces ---------------------------------------------

CONGRESO_JUNTA_URL = "https://www.congreso.es/es/junta-de-portavoces"
_JUNTA_SEARCH = (
    CONGRESO_JUNTA_URL + "?p_p_id=organos&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
    "&p_p_resource_id=searchOrgano&p_p_cacheability=cacheLevelPage"
)
BROWSER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0"


@dataclass(frozen=True)
class CongresoSeat:
    nombre: str  # "Apellidos, Nombre", as the Congreso writes it
    cargo: str  # "Portavoces titulares", "Portavoces adjuntos", …
    siglas: str
    alta: dt.date
    baja: dt.date | None
    legislatura: str


def _dmy(text: str | None) -> dt.date | None:
    if not text:
        return None
    d, m, y = (int(x) for x in text.split("/"))
    return dt.date(y, m, d)


def parse_junta(data: dict, legislatura: str) -> list[CongresoSeat]:
    return [
        CongresoSeat(
            nombre=x["apellidosNombre"],
            cargo=x["descCargo"],
            siglas=x["siglas"],
            alta=_dmy(x["fechaAltaFormat"]),  # type: ignore[arg-type]
            baja=_dmy(x.get("fechaBajaFormat")),
            legislatura=legislatura,
        )
        for x in data["data"]
        if x.get("fechaAltaFormat")
    ]


def congreso_junta(client: httpx.Client, legislatura: str = "XV") -> list[CongresoSeat]:
    """Whole history of the Junta de Portavoces of a legislature (the Congreso blocks
    generic clients, hence the browser agent)."""
    response = request_with_retries(
        client,
        "POST",
        _JUNTA_SEARCH,
        data={
            "_organos_selectedLegislatura": legislatura,
            "_organos_compoHistorica": "true",
            "_organos_selectedOrganoSup": "300",
            "_organos_selectedSuborgano": "",
        },
        headers={"User-Agent": BROWSER_AGENT},
        max_attempts=4,
        backoff=5,
        error_cls=CollectError,
    )
    if response.status_code != 200:
        raise CollectError(f"HTTP {response.status_code} en la Junta de Portavoces")
    return parse_junta(response.json(), legislatura)


# --- Dismissals named only in the body (Article 155, 2017) ---------------------

_BODY_DISMISSAL_RE = re.compile(
    # "el cese de don X como Y" or a list "el cese de: D. X, como Y; D. Z, como W"
    r"(?:(?:cese|cesar) (?:de|a):? |[:;] )(?P<art>don|doña|D\.ª?|Dª\.?) (?P<nombre>[^,;]+?),?"
    r" como (?P<cargo>[^.;,]+)"
)


def dismissals_in_body(hit: BoeHit, body: str) -> list[BoeHit]:
    """Dismissals whose names are only in the decree's text, as one hit per person."""
    prefix = _DECREE_PREFIX_RE.match(hit.title)
    if prefix is None:
        return []
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())
    return [
        BoeHit(
            hit.ident,
            hit.published,
            f"{prefix.group(1)}, por el que se dispone el cese de don"
            f" {m.group('nombre').strip()} como {m.group('cargo').strip()}.",
        )
        for m in _BODY_DISMISSAL_RE.finditer(text)
    ]
