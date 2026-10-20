"""Programas electorales de las elecciones generales (ref.programs y ref.program_chunks).

`data/knowledge/programas.yaml` dice qué programas entran (candidaturas con grupo
parlamentario propio en la legislatura siguiente) y de qué URL se descargan: solo del
dominio oficial del partido o de su copia en archive.org. `scripts/recopilar_programas.py`
descarga cada PDF, extrae el texto por página y lo guarda en data/cache/programas/ con el
SHA-256 del PDF; `conocimiento cargar-programas` lo carga como `pendiente`, y solo lo que
una persona aprueba aparece en `buscar-programa` y en el análisis.

Un programa es la postura de un partido: sirve para afirmaciones sobre lo que dice un
programa, nunca como evidencia de hechos.
"""

import datetime as dt
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import yaml
from pydantic import BaseModel, Field, model_validator

REPO = Path(__file__).resolve().parents[2]
SEED_PATH = REPO / "data" / "knowledge" / "programas.yaml"
CACHE_DIR = REPO / "data" / "cache" / "programas"
MAX_CHARS = 1500
ARCHIVE = "https://web.archive.org/web/"
_ARCHIVED = re.compile(r"^https?://web\.archive\.org/web/(\d+)(?:id_)?/(https?://.+)$")
EXCLUDED_GROUPS = ("Mixto", "Plural")


# ── semilla ──────────────────────────────────────────────────────────────────


class Election(BaseModel):
    fecha: dt.date
    legislatura: str


class Party(BaseModel):
    nombre: str
    dominios: list[str] = Field(min_length=1)


class ProgramSeed(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    eleccion: dt.date
    partido: str
    candidatura: str
    grupo: str
    idioma: str = Field(pattern=r"^[a-z]{2}$")
    url: str | None = None
    notas: str | None = None


def _host_allowed(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def official_url(url: str, domains: list[str]) -> bool:
    """The party's own domain, live or as archived by archive.org."""
    if archived := _ARCHIVED.match(url):
        return _host_allowed(archived.group(2), domains)
    return _host_allowed(url, domains)


class ProgramsSeed(BaseModel):
    elecciones: list[Election]
    partidos: dict[str, Party]
    programas: list[ProgramSeed]

    @model_validator(mode="after")
    def _consistent(self) -> "ProgramsSeed":
        dates = {e.fecha for e in self.elecciones}
        seen: set[tuple[dt.date, str]] = set()
        ids: set[str] = set()
        for p in self.programas:
            if p.id in ids:
                raise ValueError(f"{p.id}: id repetido")
            ids.add(p.id)
            if p.eleccion not in dates:
                raise ValueError(
                    f"{p.id}: la elección {p.eleccion} no está en la lista de elecciones"
                )
            if p.partido not in self.partidos:
                raise ValueError(f"{p.id}: partido desconocido «{p.partido}»")
            if p.grupo in EXCLUDED_GROUPS:
                raise ValueError(f"{p.id}: el grupo {p.grupo} no cuenta como grupo propio")
            if (p.eleccion, p.partido) in seen:
                raise ValueError(f"{p.id}: ya hay un programa de {p.partido} en {p.eleccion}")
            seen.add((p.eleccion, p.partido))
            if p.url and not official_url(p.url, self.partidos[p.partido].dominios):
                raise ValueError(
                    f"{p.id}: {p.url} no es del dominio oficial del partido "
                    f"({', '.join(self.partidos[p.partido].dominios)}) ni su copia en archive.org"
                )
        return self

    def legislature(self, election: dt.date) -> str:
        return next(e.legislatura for e in self.elecciones if e.fecha == election)


def load_seed(path: Path = SEED_PATH) -> ProgramsSeed:
    return ProgramsSeed.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# ── texto ────────────────────────────────────────────────────────────────────


class Chunk(BaseModel):
    ordinal: int
    pagina: int
    texto: str


class ProgramText(BaseModel):
    """What the collector extracts from one PDF (data/cache/programas/<id>.json)."""

    id: str
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paginas: int = Field(gt=0)
    chunks: list[Chunk]


def extract_pages(pdf: bytes) -> list[str]:
    """Text of each page (an empty string for pages without text, e.g. images)."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    if not pdf.lstrip().startswith(b"%PDF"):
        raise ValueError("El archivo descargado no es un PDF")
    try:
        reader = PdfReader(io.BytesIO(pdf))
        return [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise ValueError(f"No se pudo leer el PDF: {exc}") from exc


def _clean(text: str) -> str:
    return re.sub(r"[ \t\f\v]+", " ", text).strip()


def _pieces(page: str, max_chars: int) -> list[str]:
    """Paragraphs of a page, packed up to max_chars; an oversized paragraph is cut."""
    paragraphs = [p for p in (_clean(x) for x in re.split(r"\n\s*\n", page)) if p]
    pieces: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > max_chars:
            cut = para.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if current:
                pieces.append(current)
                current = ""
            pieces.append(para[:cut].strip())
            para = para[cut:].strip()
        if current and len(current) + 2 + len(para) > max_chars:
            pieces.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        pieces.append(current)
    return pieces


def chunk_pages(pages: list[str], max_chars: int = MAX_CHARS) -> list[Chunk]:
    """Fragments that never cross a page, so every quote keeps its page number."""
    chunks: list[Chunk] = []
    for number, page in enumerate(pages, 1):
        for piece in _pieces(page, max_chars):
            chunks.append(Chunk(ordinal=len(chunks), pagina=number, texto=piece))
    return chunks


# ── archive.org ──────────────────────────────────────────────────────────────


def archive_raw_url(timestamp: str, original: str) -> str:
    """`id_` makes archive.org return the original bytes, without its toolbar."""
    return f"{ARCHIVE}{timestamp}id_/{original}"


@dataclass
class Candidate:
    timestamp: str
    original: str
    keyword: bool  # "programa" in the file name

    @property
    def archive_url(self) -> str:
        return archive_raw_url(self.timestamp, self.original)


def wayback_candidates(rows: list[list[str]]) -> list[Candidate]:
    """PDFs from a CDX answer (first row = header), one per document, likely ones first."""
    header, *data = rows
    col = {name: i for i, name in enumerate(header)}
    seen: set[str] = set()
    found: list[Candidate] = []
    for row in data:
        if row[col["mimetype"]] != "application/pdf" or row[col["statuscode"]] != "200":
            continue
        digest = row[col["digest"]]
        if digest in seen:
            continue
        seen.add(digest)
        original = row[col["original"]]
        name = urlsplit(original).path.lower()
        found.append(Candidate(row[col["timestamp"]], original, "programa" in name))
    return sorted(found, key=lambda c: (not c.keyword, c.timestamp))


# ── base de datos ────────────────────────────────────────────────────────────


@dataclass
class LoadReport:
    new: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)  # another PDF: decide by hand
    without_url: list[str] = field(default_factory=list)
    not_collected: list[str] = field(default_factory=list)


def _title(p: ProgramSeed) -> str:
    return f"Programa electoral de {p.candidatura}: elecciones generales de {p.eleccion:%d-%m-%Y}"


def load_programs(
    conn: psycopg.Connection, seed: ProgramsSeed, cache_dir: Path = CACHE_DIR
) -> LoadReport:
    """Load every collected programme as `pendiente`, in the caller's transaction."""
    report = LoadReport()
    for p in seed.programas:
        if not p.url:
            report.without_url.append(p.id)
            continue
        path = cache_dir / f"{p.id}.json"
        if not path.exists():
            report.not_collected.append(p.id)
            continue
        text = ProgramText.model_validate_json(path.read_text(encoding="utf-8"))
        if text.url != p.url:
            report.not_collected.append(p.id)  # the seed now points to another document
            continue
        row = conn.execute("select sha256 from ref.programs where id = %s", (p.id,)).fetchone()
        if row is not None:
            (report.unchanged if row[0] == text.sha256 else report.changed).append(p.id)
            continue
        party = seed.partidos[p.partido]
        title = _title(p)
        source_id = conn.execute(
            "insert into ref.sources (organismo, titulo, url, fecha, tipo)"
            " values (%s, %s, %s, %s, 'primaria')"
            " on conflict (organismo, titulo, url) do update set organismo = excluded.organismo"
            " returning id",
            (party.nombre, title, p.url, p.eleccion),
        ).fetchone()[0]
        archived = _ARCHIVED.match(p.url)
        conn.execute(
            "insert into ref.programs (id, eleccion, candidatura, partido, grupo, titulo, idioma,"
            " url, url_original, sha256, paginas, source_id)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                p.id, p.eleccion, p.candidatura, p.partido, p.grupo, title, p.idioma, p.url,
                archived.group(2) if archived else p.url, text.sha256, text.paginas, source_id,
            ),
        )  # fmt: skip
        with conn.cursor() as cur:
            cur.executemany(
                "insert into ref.program_chunks (program_id, ordinal, pagina, texto)"
                " values (%s, %s, %s, %s)",
                [(p.id, c.ordinal, c.pagina, c.texto) for c in text.chunks],
            )
        report.new.append(p.id)
    return report


class ProgramPassage(BaseModel):
    program_id: str
    partido: str
    candidatura: str
    eleccion: dt.date
    idioma: str
    pagina: int
    url: str
    fragmento: str  # the matching words between «»
    rank: float


def search_programs(
    conn: psycopg.Connection,
    query: str,
    *,
    partido: str | None = None,
    eleccion: dt.date | None = None,
    include_pending: bool = False,
    limit: int = 10,
) -> list[ProgramPassage]:
    """Full-text search (Spanish) in the approved programmes, best matches first."""
    if not query.strip():
        return []
    states = ["aprobado", "pendiente"] if include_pending else ["aprobado"]
    rows = conn.execute(
        "with q as (select websearch_to_tsquery('spanish', %s) as q)"
        " select p.id, p.partido, p.candidatura, p.eleccion, p.idioma, c.pagina, p.url,"
        " ts_headline('spanish', c.texto, q.q, 'StartSel=«, StopSel=», MaxWords=45,"
        ' MinWords=20, MaxFragments=2, FragmentDelimiter=" … "\'),'
        " ts_rank(c.tsv, q.q)"
        " from ref.program_chunks c join ref.programs p on p.id = c.program_id, q"
        " where c.tsv @@ q.q and p.estado = any(%s)"
        " and (%s::text is null or p.partido = %s)"
        " and (%s::date is null or p.eleccion = %s)"
        " order by ts_rank(c.tsv, q.q) desc, p.eleccion desc, c.ordinal limit %s",
        (query, states, partido, partido, eleccion, eleccion, limit),
    ).fetchall()
    return [
        ProgramPassage(
            program_id=r[0], partido=r[1], candidatura=r[2], eleccion=r[3], idioma=r[4],
            pagina=r[5], url=r[6], fragmento=r[7], rank=float(r[8]),
        )
        for r in rows
    ]  # fmt: skip
