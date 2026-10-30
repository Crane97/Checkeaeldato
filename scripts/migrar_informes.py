"""Sube a Supabase los informes de data/<job_id>/ y la caché de búsquedas (una vez).

    uv run python scripts/migrar_informes.py --simular   # qué se subiría
    uv run python scripts/migrar_informes.py             # subir

Necesita SUPABASE_DB_URL, SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY en .env y las
migraciones aplicadas (`uv run conocimiento migrar`). Los informes entran como borrador; lo
que ya está en Supabase no se toca, así que se puede repetir.
"""

import logging
import sys
from pathlib import Path
from typing import Annotated

import psycopg
import typer

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline.config import ConfigError, get_settings  # noqa: E402
from pipeline.evidence.search_cache import DbSearchStore  # noqa: E402
from pipeline.reports.files import StorageError  # noqa: E402
from pipeline.reports.migrate_local import migrate_reports, migrate_search_cache  # noqa: E402
from pipeline.reports.safe_errors import install_secret_filter, secret_values  # noqa: E402
from pipeline.reports.sync import ReportUploader  # noqa: E402

app = typer.Typer(add_completion=False)


@app.command()
def main(
    simular: Annotated[bool, typer.Option("--simular", help="Solo muestra qué se subiría")] = False,
) -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    install_secret_filter(settings)
    try:
        uploader = ReportUploader.open(settings, origen="local")
    except (ConfigError, StorageError, psycopg.Error) as exc:
        typer.secho(f"Error: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    url = settings.supabase_db_url.get_secret_value()  # type: ignore[union-attr]
    try:
        with psycopg.connect(url, connect_timeout=20) as conn:
            result = migrate_reports(
                conn, uploader, settings.data_dir, simulate=simular,
                secrets=secret_values(settings),
            )  # fmt: skip
            store = DbSearchStore(lambda: psycopg.connect(url, connect_timeout=10), strict=True)
            migrate_search_cache(
                store, settings.data_dir / "cache" / "search", result, simulate=simular
            )
    except psycopg.Error as exc:
        typer.secho(f"Error de la base de datos; se paró la migración: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        uploader.close()
    verb = "Se subirían" if simular else "Subidos"
    typer.echo(f"{verb} como borrador: {len(result.uploaded)} informes")
    for job_id in result.uploaded:
        typer.echo(f"   {job_id}")
    if result.retried:
        typer.echo(f"Reintentados (su subida anterior había fallado): {', '.join(result.retried)}")
    typer.echo(f"Ya estaban en Supabase: {len(result.already)}")
    typer.echo(f"Análisis sin terminar (no se suben): {len(result.unfinished)}")
    typer.echo(
        f"Búsquedas en caché {'por copiar' if simular else 'copiadas'}: {result.cache_copied}"
        f" (ya estaban: {result.cache_present})"
    )
    for line in result.failed:
        typer.secho(f"   No se pudo subir {line}", fg="red")
    if result.uploaded and not simular:
        typer.echo("\nTítulo y tipo de evento se completan en el panel antes de publicar.")
    if result.failed:
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
