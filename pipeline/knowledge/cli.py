"""Comandos de la base de conocimiento: `uv run conocimiento <comando>`."""

from typing import Annotated

import psycopg
import typer

from pipeline.config import ConfigError, get_settings
from pipeline.knowledge.catalog import load_catalog, sync_catalog
from pipeline.knowledge.governments_load import load_governments
from pipeline.knowledge.migrate import MigrationError, apply, load_migrations, pending
from pipeline.knowledge.observations_load import load_json_base

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


@app.command("cargar-gobiernos")
def cargar_gobiernos(
    actualizar: Annotated[
        bool,
        typer.Option(
            "--actualizar", help="Sobrescribe lo que cambió en el JSON (vuelve a revisión)"
        ),
    ] = False,
) -> None:
    """Carga data/knowledge/governments.json (entran pendientes de revisión), todo o nada."""
    with _connect() as conn:
        report = load_governments(conn, update=actualizar)
        conn.commit()
    typer.echo(
        f"Nuevos: {len(report.new)} · sin cambios: {len(report.unchanged)} · "
        f"cambiados en el JSON (no sobrescritos): {len(report.changed)} · "
        f"actualizados y devueltos a revisión: {len(report.updated)}"
    )
    for item in report.changed:
        typer.secho(f"  Cambiado, revisar a mano: {item}", fg=typer.colors.YELLOW)


@app.command()
def catalogo() -> None:
    """Sincroniza catalog.yaml con ref.indicators (unidad, definición y frecuencia no cambian)."""
    try:
        with _connect() as conn:
            report = sync_catalog(conn, load_catalog())
            conn.commit()
    except (ValueError, psycopg.Error) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Nuevos: {len(report.new)} · actualizados: {len(report.updated)} · "
        f"sin cambios: {len(report.unchanged)}"
    )


@app.command("cargar-json")
def cargar_json() -> None:
    """Carga inicial de data/knowledge/indicators/ en ref.observations (validada), todo o nada."""
    import datetime as dt

    with _connect() as conn:
        summaries = load_json_base(conn, load_catalog(), today=dt.date.today())
        conn.commit()
    for s in summaries:
        typer.echo(
            f"{s.indicator_id:24} nuevas {s.new:4} · revisiones {s.revisions:3} · "
            f"sin cambios {s.unchanged:4} · cuarentena {s.quarantine:3} · pendientes {s.pending:3}"
        )
        for e in s.errors:
            typer.secho(f"   error: {e}", fg=typer.colors.RED)
