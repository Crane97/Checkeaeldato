"""Comandos de la base de conocimiento: `uv run conocimiento <comando>`."""

from typing import Annotated

import psycopg
import typer

from pipeline.config import ConfigError, get_settings
from pipeline.knowledge.migrate import MigrationError, apply, load_migrations, pending

app = typer.Typer(no_args_is_help=True, help="Base de conocimiento oficial (Supabase Postgres).")


def _connect() -> psycopg.Connection:
    settings = get_settings()
    if settings.supabase_db_url is None:
        raise ConfigError("Falta SUPABASE_DB_URL en .env (proyecto de DESARROLLO de Supabase)")
    return psycopg.connect(settings.supabase_db_url.get_secret_value(), connect_timeout=20)


@app.command()
def migrar(
    simular: Annotated[
        bool, typer.Option("--simular", help="Muestra lo pendiente sin aplicar nada")
    ] = False,
) -> None:
    """Aplica las migraciones pendientes de db/migrations/, cada una en su transacción."""
    try:
        migrations = load_migrations()
        with _connect() as conn:
            todo = pending(conn, migrations)
            if simular:
                conn.rollback()  # not even the ledger table is created
            else:
                conn.commit()  # the ledger table, if it did not exist
            if not todo:
                typer.echo("No hay migraciones pendientes.")
                return
            for m in todo:
                if simular:
                    typer.echo(f"Pendiente: {m.name}")
                    continue
                apply(conn, m)
                conn.commit()
                typer.echo(f"Aplicada: {m.name}")
    except (MigrationError, ConfigError, psycopg.Error) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def version() -> None:
    """Muestra las migraciones aplicadas."""
    with _connect() as conn:
        rows = conn.execute(
            "select version, nombre, aplicada_en from ops.schema_migrations order by version"
        ).fetchall()
    for v, name, when in rows:
        typer.echo(f"{v}  {name}  ({when:%Y-%m-%d %H:%M})")
