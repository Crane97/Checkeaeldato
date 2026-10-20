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
"""

import datetime as dt
import hashlib
import sys
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline.knowledge.programs import (  # noqa: E402
    CACHE_DIR,
    ProgramText,
    chunk_pages,
    extract_pages,
    load_seed,
    wayback_candidates,
)

CDX = "https://web.archive.org/cdx/search/cdx"
DAYS_BEFORE = 75
PAUSE_SECONDS = 2.0
MAX_CANDIDATES = 15
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


def _buscar(client: httpx.Client, solo: set[str] | None) -> None:
    seed = load_seed()
    for p in seed.programas:
        if (solo and p.id not in solo) or (not solo and p.url):
            continue
        typer.secho(f"\n{p.id} · {p.candidatura} · {p.eleccion}", bold=True)
        for domain in seed.partidos[p.partido].dominios:
            try:
                rows = _search(client, domain, p.eleccion)
            except (httpx.HTTPError, ValueError) as exc:
                typer.secho(f"   {domain}: error al consultar archive.org: {exc}", fg="red")
                continue
            finally:
                time.sleep(PAUSE_SECONDS)
            found = wayback_candidates(rows) if rows else []
            if not found:
                typer.echo(f"   {domain}: ningún PDF archivado en esas fechas")
            for c in found[:MAX_CANDIDATES]:
                mark = "★" if c.keyword else " "
                typer.echo(f"   {mark} {c.timestamp[:8]}  {c.archive_url}")
            if len(found) > MAX_CANDIDATES:
                typer.echo(f"     … y {len(found) - MAX_CANDIDATES} más")


def _download(client: httpx.Client, url: str) -> bytes:
    response = client.get(url, timeout=300, follow_redirects=True)
    response.raise_for_status()
    return response.content


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
        if out.exists() and ProgramText.model_validate_json(out.read_text()).url == p.url:
            typer.echo(f"{p.id:16} ya recopilado")
            continue
        try:
            pdf = _download(client, p.url)
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
        out.write_text(text.model_dump_json(), encoding="utf-8")
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
            _buscar(client, set(solo) if solo else None)
            return
        failures = _recopilar(client, set(solo) if solo else None)
    if failures:
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
