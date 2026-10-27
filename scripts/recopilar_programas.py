"""Descarga los programas electorales de data/knowledge/programas.yaml y extrae su texto.

    uv run python scripts/recopilar_programas.py --buscar             # proponer URLs
    uv run python scripts/recopilar_programas.py --buscar --solo pp-2016-06
    uv run python scripts/recopilar_programas.py                      # descargar y extraer

--buscar consulta el índice de archive.org (CDX, gratuito y sin clave) y lista los PDF
guardados del DOMINIO OFICIAL de cada partido en los 75 días anteriores a la votación,
primero los que llevan "programa" en el nombre. Elige el bueno, ábrelo para comprobar que es
el programa completo y fija su URL en programas.yaml.

Sin --buscar, descarga cada programa con URL, comprueba que es un PDF, calcula su SHA-256,
extrae el texto por página y lo guarda en data/cache/programas/<id>.json. Después:
`uv run conocimiento cargar-programas` y `uv run conocimiento revisar --tipo programa`.

La descarga sigue como mucho MAX_REDIRECTS redirecciones, y cada una debe quedarse en el
dominio oficial del partido o en web.archive.org, por https; se corta en MAX_PDF_BYTES.
"""

import datetime as dt
import hashlib
import sys
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urljoin, urlsplit

import httpx
import typer
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline.knowledge.programs import (  # noqa: E402
    CACHE_DIR,
    ProgramText,
    chunk_pages,
    extract_pages,
    load_seed,
    official_url,
    wayback_candidates,
)

CDX = "https://web.archive.org/cdx/search/cdx"
DAYS_BEFORE = 75
PAUSE_SECONDS = 2.0
MAX_CANDIDATES = 15
MAX_REDIRECTS = 5
MAX_PDF_BYTES = 80 * 1024 * 1024
HEADERS = {"User-Agent": "checkea-el-dato/0.1 (recopilación de programas electorales)"}

app = typer.Typer(add_completion=False)


def _search(client: httpx.Client, domain: str, election: dt.date) -> list[list[str]]:
    start = election - dt.timedelta(days=DAYS_BEFORE)
    response = client.get(
        CDX,
        params={
            "url": domain,
            "matchType": "domain",
            "filter": "mimetype:application/pdf",
            "from": f"{start:%Y%m%d}",
            "to": f"{election:%Y%m%d}",
            "fl": "timestamp,original,mimetype,statuscode,digest",
            "collapse": "digest",
            "output": "json",
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json() if response.content.strip() else []


def _buscar(client: httpx.Client, solo: set[str] | None) -> int:
    """Lists candidates; returns how many programmes could not be searched at all."""
    seed = load_seed()
    unsearched = 0
    for p in seed.programas:
        if (solo and p.id not in solo) or (not solo and p.url):
            continue
        typer.secho(f"\n{p.id} · {p.candidatura} · {p.eleccion}", bold=True)
        domains = seed.partidos[p.partido].dominios
        errors = 0
        for domain in domains:
            try:
                rows = _search(client, domain, p.eleccion)
                found = wayback_candidates(rows) if rows else []
            except (httpx.HTTPError, ValueError) as exc:
                typer.secho(f"   {domain}: error al consultar archive.org: {exc}", fg="red")
                errors += 1
                continue
            finally:
                time.sleep(PAUSE_SECONDS)
            if not found:
                typer.echo(f"   {domain}: ningún PDF archivado en esas fechas")
            for c in found[:MAX_CANDIDATES]:
                mark = "★" if c.keyword else " "
                typer.echo(f"   {mark} {c.timestamp[:8]}  {c.archive_url}")
            if len(found) > MAX_CANDIDATES:
                typer.echo(f"     … y {len(found) - MAX_CANDIDATES} más")
        if errors == len(domains):
            unsearched += 1  # not "no PDF": archive.org could not be asked
    return unsearched


def _allowed(url: str, domains: list[str]) -> bool:
    """Every hop stays on the party's domain, or on archive.org's copy of a page of that
    domain, over https on the default port and without credentials in the URL."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.username:
        return False
    return official_url(url, domains)


def _download(client: httpx.Client, url: str, domains: list[str]) -> bytes:
    for _ in range(MAX_REDIRECTS + 1):
        if not _allowed(url, domains):
            raise ValueError(f"URL fuera del dominio oficial o de archive.org: {url}")
        with client.stream("GET", url, timeout=120, follow_redirects=False) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > MAX_PDF_BYTES:
                raise ValueError(f"El archivo ocupa {declared} bytes (máximo {MAX_PDF_BYTES})")
            data = bytearray()
            for chunk in response.iter_bytes():
                if not data and not chunk.lstrip().startswith(b"%PDF"):
                    raise ValueError("El archivo descargado no es un PDF")
                data += chunk
                if len(data) > MAX_PDF_BYTES:
                    raise ValueError(f"El archivo supera {MAX_PDF_BYTES} bytes")
            return bytes(data)
    raise ValueError(f"Demasiadas redirecciones (más de {MAX_REDIRECTS})")


def _already_collected(out: Path, url: str) -> bool:
    """A truncated or invalid cache file counts as not collected (it is downloaded again)."""
    if not out.exists():
        return False
    try:
        return ProgramText.model_validate_json(out.read_text()).url == url
    except (OSError, ValidationError):
        return False


def _recopilar(client: httpx.Client, solo: set[str] | None) -> int:
    seed = load_seed()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for p in seed.programas:
        if solo and p.id not in solo:
            continue
        if not p.url:
            typer.echo(f"{p.id:16} sin URL (usa --buscar)")
            continue
        out = CACHE_DIR / f"{p.id}.json"
        if _already_collected(out, p.url):
            typer.echo(f"{p.id:16} ya recopilado")
            continue
        try:
            pdf = _download(client, p.url, seed.partidos[p.partido].dominios)
            pages = extract_pages(pdf)
        except (httpx.HTTPError, ValueError) as exc:
            typer.secho(f"{p.id:16} ERROR: {exc}", fg="red")
            failures += 1
            continue
        finally:
            time.sleep(PAUSE_SECONDS)
        chunks = chunk_pages(pages)
        if not chunks:
            typer.secho(
                f"{p.id:16} ERROR: el PDF no tiene texto (¿escaneado? no se hace OCR)", fg="red"
            )
            failures += 1
            continue
        text = ProgramText(
            id=p.id,
            url=p.url,
            sha256=hashlib.sha256(pdf).hexdigest(),
            paginas=len(pages),
            chunks=chunks,
        )
        tmp = out.with_suffix(".tmp")
        tmp.write_text(text.model_dump_json(), encoding="utf-8")
        tmp.replace(out)  # atomic: never a half-written cache file
        empty = len(pages) - len({c.pagina for c in chunks})
        warning = f" · {empty} páginas sin texto" if empty else ""
        typer.echo(f"{p.id:16} {len(pages)} páginas · {len(chunks)} fragmentos{warning}")
    return failures


@app.command()
def main(
    buscar: Annotated[
        bool, typer.Option("--buscar", help="Proponer URLs desde archive.org")
    ] = False,
    solo: Annotated[list[str] | None, typer.Option("--solo", help="Solo estos ids")] = None,
) -> None:
    with httpx.Client(headers=HEADERS) as client:
        if buscar:
            if _buscar(client, set(solo) if solo else None):
                raise typer.Exit(code=2)
            return
        failures = _recopilar(client, set(solo) if solo else None)
    if failures:
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
