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
    approve_official,
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
        typer.Option(
            "--tipo", help="observacion, gobierno, partido, hecho, politico o programa (repetible)"
        ),
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


@app.command("cargar-programas")
def cargar_programas() -> None:
    """Carga los programas electorales recopilados (entran pendientes de revisión), todo o nada.

    Antes: `uv run python scripts/recopilar_programas.py` (descarga y extrae el texto).
    """
    from pipeline.knowledge.programs import load_programs, load_seed

    try:
        with _connect() as conn:
            report = load_programs(conn, load_seed())
            conn.commit()
    except (ValueError, psycopg.Error) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Nuevos: {len(report.new)} · sin cambios: {len(report.unchanged)} · "
        f"sin URL en programas.yaml: {len(report.without_url)} · "
        f"sin recopilar: {len(report.not_collected)}"
    )
    for pid in report.changed:
        typer.secho(
            f"  Otro PDF que el ya cargado (decide a mano; no se sobrescribe): {pid}",
            fg=typer.colors.YELLOW,
        )
    for pid in report.not_collected:
        typer.secho(f"  Sin recopilar con la URL actual: {pid}", fg=typer.colors.YELLOW)


@app.command("buscar-programa")
def buscar_programa(
    texto: Annotated[
        str, typer.Argument(help='Palabras a buscar (admite "frase exacta" y -excluir)')
    ],
    partido: Annotated[
        str | None, typer.Option("--partido", help="Id del partido en programas.yaml (pp, psoe…)")
    ] = None,
    eleccion: Annotated[
        str | None, typer.Option("--eleccion", help="Fecha de la elección, AAAA-MM-DD")
    ] = None,
    pendientes: Annotated[
        bool, typer.Option("--pendientes", help="Incluye los programas aún sin revisar")
    ] = False,
    limite: Annotated[int, typer.Option("--limite", help="Número de resultados")] = 10,
) -> None:
    """Busca en el texto de los programas electorales (solo los aprobados, salvo --pendientes)."""
    import datetime as dt

    from pipeline.knowledge.programs import search_programs

    try:
        date = dt.date.fromisoformat(eleccion) if eleccion else None
    except ValueError as exc:
        typer.secho(f"Fecha no válida: {eleccion}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    with _connect() as conn:
        found = search_programs(
            conn, texto, partido=partido, eleccion=date, include_pending=pendientes, limit=limite
        )
        conn.rollback()
    if not found:
        typer.echo("Sin resultados.")
        return
    for r in found:
        typer.secho(
            f"\n{r.candidatura} · {r.eleccion:%Y-%m-%d} · pág. {r.pagina} ({r.program_id})",
            bold=True,
        )
        typer.echo(f"   {r.fragmento}")
        typer.echo(f"   {r.url}")
    if any(r.idioma != "es" for r in found):
        typer.echo(
            "\nLa búsqueda usa el diccionario español: en los programas en catalán, euskera o"
            " gallego puede no encontrar todo."
        )


@app.command()
def retirar(
    tipo: Annotated[str, typer.Argument(help="gobierno, partido, hecho, politico o programa")],
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
    territorial: Annotated[
        bool,
        typer.Option(
            "--territorial",
            help="También las series por comunidad y provincia (antes: conocimiento espacio)",
        ),
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
            if territorial:
                from pipeline.knowledge.ingest import ingest_territorial
                from pipeline.knowledge.territories import load_territories

                extra = ingest_territorial(
                    conn,
                    load_catalog(),
                    client,
                    today=today,
                    territories=load_territories(),
                    only=set(indicador) if indicador else None,
                    force=forzar,
                    simulate=simular,
                )
                report.results += extra.results
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


@app.command()
def cobertura(
    desde: Annotated[
        str | None, typer.Option("--desde", help="Solo veredictos desde esta fecha (AAAA-MM-DD)")
    ] = None,
) -> None:
    """Cobertura de la base por partido y tema: ¿ayuda por igual a todos?"""
    import datetime as dt

    from pipeline.knowledge.coverage import coverage

    since = (
        dt.datetime.combine(dt.date.fromisoformat(desde), dt.time(), tzinfo=dt.UTC)
        if desde
        else None
    )
    with _connect() as conn:
        report = coverage(conn, since)
        conn.rollback()
    if not report.parties:
        typer.echo("No hay veredictos guardados todavía.")
        return
    for title, groups in (("Por partido", report.parties), ("Por tema", report.topics)):
        typer.secho(f"\n{title}", bold=True)
        for g in groups:
            typer.echo(
                f"  {g.name:32} {g.total:4} veredictos · con datos de series {g.pct_data:3.0f} %"
                f" · no verificable {g.pct_unverifiable:3.0f} %"
            )
    for line in report.alerts:
        typer.secho(f"\nAlerta: {line}", fg=typer.colors.YELLOW)
    for line in report.gaps:
        typer.secho(f"Tema sin indicadores suficientes: {line}", fg=typer.colors.YELLOW)
    for line in report.excluded:
        typer.echo(f"Fuera de la comparación: {line}")


@app.command("aprobar-oficiales")
def aprobar_oficiales() -> None:
    """Aprueba los datos pendientes cuya fuente es oficial primaria (BOE, INE, INSS…).
    La cuarentena no se toca: es el control de anomalías."""
    with _connect() as conn:
        counts = approve_official(conn)
        conn.commit()
    if not counts:
        typer.echo("No había datos oficiales pendientes.")
    for indicator_id, n in sorted(counts.items()):
        typer.echo(f"{indicator_id:24} aprobados {n}")


@app.command("cargar-territorios")
def cargar_territorios() -> None:
    """Carga data/knowledge/territorios.json (comunidades y provincias del INE)."""
    from pipeline.knowledge.territories import load_territories, sync_territories

    with _connect() as conn:
        n = sync_territories(conn, load_territories())
        conn.commit()
    typer.echo(f"{n} territorios cargados o actualizados.")


FREE_PLAN_MB = 500


@app.command()
def espacio() -> None:
    """Tamaño actual de la base y estimación si se cargan las series territoriales."""
    from pipeline.knowledge.territories import load_territories

    catalog = load_catalog()
    levels: dict[str, int] = {}
    for t in load_territories():
        levels[t.nivel] = levels.get(t.nivel, 0) + 1
    with _connect() as conn:
        size_mb = conn.execute("select pg_database_size(current_database())").fetchone()[0] / 1e6
        rows, rel_bytes = conn.execute(
            "select count(*), pg_total_relation_size('ref.observations') from ref.observations"
        ).fetchone()
        per_row = rel_bytes / rows if rows else 400
        national = dict(
            conn.execute(
                "select indicator_id, count(*) from ref.observations"
                " where territorio_code = 'ES' and vigente group by 1"
            ).fetchall()
        )
        conn.rollback()
    typer.echo(f"Base de datos: {size_mb:.1f} MB de {FREE_PLAN_MB} MB del plan Free")
    typer.echo(f"Observaciones: {rows} filas, ~{per_row:.0f} bytes por fila con índices")
    extra_rows = 0
    for spec in catalog.values():
        for src in spec.territorial:
            n = national.get(spec.id) or 30  # no national series: ~30 periods
            est = n * levels.get(src.nivel, 0)
            extra_rows += est
            typer.echo(f"  {spec.id}@{src.nivel}: ~{est} filas")
    extra_mb = extra_rows * per_row / 1e6
    typer.echo(
        f"Territorial: ~{extra_rows} filas, ~{extra_mb:.1f} MB → total ~{size_mb + extra_mb:.1f} MB"
        f" ({(size_mb + extra_mb) / FREE_PLAN_MB:.0%} del plan Free)"
    )
