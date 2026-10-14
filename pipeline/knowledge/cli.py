"""Comandos de la base de conocimiento: `uv run conocimiento <comando>`."""

from typing import Annotated

import psycopg
import typer

from pipeline.config import ConfigError, get_settings
from pipeline.knowledge.catalog import load_catalog, sync_catalog
from pipeline.knowledge.governments_load import load_governments
from pipeline.knowledge.impact import dependents, observation_summary, reprocess_commands
from pipeline.knowledge.ingest import IngestError, IngestReport, ingest
from pipeline.knowledge.migrate import MigrationError, apply, load_migrations, pending
from pipeline.knowledge.observations_load import load_json_base
from pipeline.knowledge.politicians_repo import load_politicians
from pipeline.knowledge.review import (
    KINDS,
    ReviewError,
    ReviewItem,
    decide,
    pending_items,
    withdraw,
)

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


_KEYS = "[a]probar · [r]echazar · [s]altar · [t]odos los de este grupo · [q] salir"


def _show(item: ReviewItem, n: int, total: int) -> None:
    typer.secho(f"\n[{n}/{total}] {item.kind} {item.id} · {item.title}", bold=True)
    for line in item.lines:
        typer.echo(f"   {line}")


@app.command()
def revisar(
    tipo: Annotated[
        list[str] | None,
        typer.Option("--tipo", help="observacion, gobierno, partido o hecho (repetible)"),
    ] = None,
    indicador: Annotated[
        str | None, typer.Option("--indicador", help="Solo las observaciones de un indicador")
    ] = None,
    lista: Annotated[bool, typer.Option("--lista", help="Solo muestra lo pendiente")] = False,
    revisor: Annotated[
        str | None, typer.Option("--revisor", help="Tu nombre: queda registrado en cada decisión")
    ] = None,
) -> None:
    """Muestra lo pendiente con su fuente y pide aprobarlo o rechazarlo (solo lo aprobado se usa).

    Antes de aprobar datos curados a mano, pásalos por el agente data-verifier.
    """
    kinds = tuple(tipo) if tipo else KINDS
    if unknown := set(kinds) - set(KINDS):
        typer.secho(f"Tipo no válido: {', '.join(sorted(unknown))}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    with _connect() as conn:
        items = pending_items(conn, kinds, indicator=indicador)  # type: ignore[arg-type]
        conn.rollback()
        if not items:
            typer.echo("No hay nada pendiente de revisión.")
            return
        if lista:
            for n, item in enumerate(items, 1):
                _show(item, n, len(items))
            return
        name = revisor or typer.prompt("Tu nombre (queda registrado como revisor)")
        counts = {"aprobado": 0, "rechazado": 0}
        bulk: set[str] = set()
        for n, item in enumerate(items, 1):
            _show(item, n, len(items))
            if item.group in bulk:
                answer = "a"
            else:
                answer = typer.prompt(_KEYS, default="s").strip().lower()[:1]
                if answer == "q":
                    break
                if answer == "t":
                    rest = sum(1 for i in items[n - 1 :] if i.group == item.group)
                    if typer.confirm(f"¿Aprobar este y los {rest - 1} siguientes de {item.group}?"):
                        bulk.add(item.group)
                        answer = "a"
                    else:
                        answer = "s"
            if answer not in ("a", "r"):
                continue
            outcome = "aprobado" if answer == "a" else "rechazado"
            try:
                decide(conn, item.kind, item.id, outcome, reviewer=name)
                conn.commit()
                counts[outcome] += 1
            except (ReviewError, psycopg.Error) as exc:
                conn.rollback()
                typer.secho(f"   No se pudo guardar: {exc}", fg=typer.colors.RED)
    typer.echo(f"\nAprobados: {counts['aprobado']} · rechazados: {counts['rechazado']}")


@app.command()
def impacto(observation_id: Annotated[int, typer.Argument(help="Id de ref.observations")]) -> None:
    """Lista los veredictos que usaron un dato y el comando para recalcular cada trabajo."""
    with _connect() as conn:
        summary = observation_summary(conn, observation_id)
        if summary is None:
            typer.secho(f"No existe la observación {observation_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        found = dependents(conn, observation_id)
        conn.rollback()
    typer.echo(f"Dato: {summary}")
    if not found:
        typer.echo("Ningún veredicto guardado usa este dato.")
        return
    typer.echo(f"Veredictos que dependen de él: {len(found)}")
    for d in found:
        typer.echo(
            f"  {d.fecha_verificacion:%Y-%m-%d} · trabajo {d.job_id or '—'} · {d.claim_id or '—'}"
            f" · {d.veredicto} · {d.claim_normalizado}"
        )
    typer.echo("\nPara recalcularlos desde la evidencia (gasta búsquedas y modelo):")
    for command in reprocess_commands(d.job_id for d in found):
        typer.echo(f"  {command}")


@app.command("cargar-politicos")
def cargar_politicos(
    actualizar: Annotated[
        bool,
        typer.Option(
            "--actualizar", help="Sobrescribe lo que cambió en el JSON (vuelve a revisión)"
        ),
    ] = False,
) -> None:
    """Carga data/knowledge/politicians.json (entran pendientes de revisión), todo o nada."""
    try:
        with _connect() as conn:
            report = load_politicians(conn, update=actualizar)
            conn.commit()
    except (ValueError, psycopg.Error) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Nuevos: {len(report.new)} · sin cambios: {len(report.unchanged)} · "
        f"cambiados en el JSON (no sobrescritos): {len(report.changed)} · "
        f"actualizados y devueltos a revisión: {len(report.updated)}"
    )
    for pid in report.changed:
        typer.secho(f"  Cambiado, usa --actualizar o revisa a mano: {pid}", fg=typer.colors.YELLOW)
    for pid in report.missing:
        typer.secho(
            f"  Ya no está en el JSON (recházalo en `revisar`): {pid}", fg=typer.colors.YELLOW
        )
    for line in report.previous_reviews:
        typer.echo(f"  Revisión anterior (vuelve a pendiente): {line}")


@app.command()
def retirar(
    tipo: Annotated[str, typer.Argument(help="gobierno, partido, hecho o politico")],
    item_id: Annotated[str, typer.Argument(help="Id del elemento aprobado")],
    revisor: Annotated[
        str | None, typer.Option("--revisor", help="Tu nombre: queda registrado")
    ] = None,
) -> None:
    """Rechaza algo que ya estaba aprobado: deja de usarse en el siguiente análisis."""
    name = revisor or typer.prompt("Tu nombre (queda registrado como revisor)")
    if not typer.confirm(f"¿Retirar {tipo} {item_id}? Dejará de usarse"):
        raise typer.Exit(code=1)
    try:
        with _connect() as conn:
            estado, antes = withdraw(conn, tipo, item_id, reviewer=name)  # type: ignore[arg-type]
            conn.commit()
    except (ReviewError, psycopg.Error) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Retirado {tipo} {item_id} (estaba {estado}, revisado por {antes}).")


def _alert(report: IngestReport, today) -> None:
    """Email the report to ALERT_EMAIL_TO (SMTP settings of .env)."""
    from email.message import EmailMessage

    from pipeline.email_report import send_email

    settings = get_settings()
    if not settings.alert_email_to:
        raise ConfigError("Falta ALERT_EMAIL_TO en .env para enviar el aviso")
    message = EmailMessage()
    message["Subject"] = f"Checkea el Dato · ingesta {today}: hay que revisar datos"
    message["From"] = settings.email_from or settings.smtp_user or ""
    message["To"] = settings.alert_email_to
    lines = ["La ingesta automática ha encontrado algo que necesita revisión:", ""]
    lines += [f"Error: {e}" for e in report.errors]
    lines += [f"En cuarentena: {q}" for q in report.quarantine]
    lines += [f"Desactualizado: {s}" for s in report.stale]
    lines += ["", "Detalle:", *report.lines(), "", "Revisa con `uv run conocimiento revisar`."]
    message.set_content("\n".join(lines))
    send_email(message, settings)


@app.command()
def ingerir(
    simular: Annotated[
        bool, typer.Option("--simular", help="Descarga y valida, pero no guarda nada")
    ] = False,
    indicador: Annotated[
        list[str] | None, typer.Option("--indicador", help="Solo estos indicadores (repetible)")
    ] = None,
    forzar: Annotated[
        bool, typer.Option("--forzar", help="Carga aunque la huella no haya cambiado")
    ] = False,
    avisar: Annotated[
        bool,
        typer.Option("--avisar", help="Envía un correo si hay errores, cuarentena o datos viejos"),
    ] = False,
) -> None:
    """Descarga las series automáticas (INE, Eurostat, Seguridad Social) y carga lo nuevo."""
    import datetime as dt

    import httpx

    today = dt.date.today()
    try:
        with _connect() as conn, httpx.Client(timeout=60, follow_redirects=True) as client:
            report = ingest(
                conn,
                load_catalog(),
                client,
                today=today,
                only=set(indicador) if indicador else None,
                force=forzar,
                simulate=simular,
            )
            if simular:
                conn.rollback()
            else:
                conn.commit()
    except (IngestError, ConfigError, psycopg.Error) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if simular:
        typer.secho("SIMULACIÓN: no se ha guardado nada.", fg=typer.colors.YELLOW)
    for line in report.lines():
        typer.echo(line)
    if report.needs_attention():
        typer.secho("Hay datos que revisar.", fg=typer.colors.YELLOW)
        if avisar:
            _alert(report, today)
            typer.echo("Aviso enviado.")
    if report.errors:
        raise typer.Exit(code=2)
