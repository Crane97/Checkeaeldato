"""Entrada de línea de comandos: `uv run verificar <video|texto> --fecha AAAA-MM-DD`."""

import datetime as dt
import logging
from pathlib import Path
from typing import Annotated, cast

import psycopg
import typer
from pydantic import TypeAdapter, ValidationError

from pipeline.audio import AudioError
from pipeline.claims import ClaimsError
from pipeline.config import ConfigError, get_settings
from pipeline.email_report import EmailError, email_report, is_valid_email
from pipeline.evidence.base import EvidenceError
from pipeline.jobs import JobError
from pipeline.llm import LLMError
from pipeline.models import STEPS, SpeakerInfo, StepName
from pipeline.report import ReportError
from pipeline.reports.files import StorageError
from pipeline.reports.repository import EventType
from pipeline.reports.repository import ReportError as StoredReportError
from pipeline.reports.safe_errors import install_secret_filter, public_message, secret_values
from pipeline.reports.sync import ReportUploader, job_id_for
from pipeline.run import LiveDeps, input_kind_for, run_job
from pipeline.text_input import InputError
from pipeline.transcribe import TranscriptionError
from pipeline.verdict import VerdictError

app = typer.Typer(add_completion=False)

# Errors with a message meant for the user; anything else shows a traceback.
USER_ERRORS = (
    ConfigError,
    JobError,
    InputError,
    AudioError,
    TranscriptionError,
    LLMError,
    ClaimsError,
    EvidenceError,
    VerdictError,
    ReportError,
    FileNotFoundError,
    ValueError,
)


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("usa el formato AAAA-MM-DD, p. ej. 2026-11-20") from exc


def _parse_speakers(value: str | None) -> dict[str, str] | None:
    if value is None:
        return None
    speakers: dict[str, str] = {}
    for pair in value.split(","):
        label, sep, name = pair.partition("=")
        if not sep or not label.strip() or not name.strip():
            raise typer.BadParameter('usa el formato "speaker_0=Nombre,speaker_1=Nombre"')
        speakers[label.strip()] = name.strip()
    return speakers


def _parse_speakers_file(path: Path | None) -> dict[str, SpeakerInfo] | None:
    if path is None:
        return None
    try:
        return TypeAdapter(dict[str, SpeakerInfo]).validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise typer.BadParameter(
            f"no se pudo leer {path}: debe ser un JSON como "
            '{"SPEAKER_0": {"name": "Ana Ruiz", "party": "PSOE", "role": "candidata"}}',
            param_hint="--speakers",
        ) from exc


def _parse_step(value: str | None) -> StepName | None:
    if value is not None and value not in STEPS:
        raise typer.BadParameter(f"paso desconocido; usa uno de: {', '.join(STEPS)}")
    return cast(StepName | None, value)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


EVENT_TYPES: tuple[EventType, ...] = ("debate", "mitin", "entrevista", "sesion_parlamentaria")
UPLOAD_ERRORS = (ConfigError, StorageError, StoredReportError, psycopg.Error, OSError)


def open_uploader(settings) -> ReportUploader:
    return ReportUploader.open(settings, origen="local")


def _parse_event_type(tipo: str | None, subir: bool, titulo: str | None) -> EventType | None:
    if not subir and (tipo is not None or titulo is not None):
        raise typer.BadParameter("--titulo y --tipo solo se usan con --subir", param_hint="--subir")
    if tipo is not None and tipo not in EVENT_TYPES:
        raise typer.BadParameter(f"usa uno de: {', '.join(EVENT_TYPES)}", param_hint="--tipo")
    return cast(EventType | None, tipo)


def _begin_upload(settings, entrada: Path, speech_date: dt.date, titulo, tipo) -> ReportUploader:
    """Register the analysis in Supabase before starting, so a bad configuration fails now."""
    try:
        uploader = open_uploader(settings)
        uploader.begin(
            job_id=job_id_for(entrada), titulo=titulo or entrada.stem, fecha=speech_date,
            input_kind=input_kind_for(entrada), fuente=entrada.name, tipo=tipo,
        )  # fmt: skip
    except (*UPLOAD_ERRORS, FileNotFoundError, ValueError) as exc:
        typer.secho(f"\nNo se pudo registrar el análisis en Supabase: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    return uploader


def _record_failure(uploader: ReportUploader, job_id: str, message: str) -> None:
    try:
        uploader.fail(job_id, message)
    except UPLOAD_ERRORS as exc:
        typer.secho(f"No se pudo anotar el error en Supabase: {exc}", fg="red", err=True)
    finally:
        uploader.close()


def _finish_upload(
    uploader: ReportUploader, job_dir: Path, report, secrets: list[str] | None = None
) -> None:
    try:
        meta = uploader.finish(job_dir, report)
    except UPLOAD_ERRORS as exc:
        typer.secho(
            f"\nEl análisis terminó, pero no se pudo subir a Supabase: {exc}\n"
            "Repite el mismo comando con --subir: no se vuelve a analizar nada.",
            fg="red",
            err=True,
        )
        # The panel must not keep showing it "en análisis".
        _record_failure(
            uploader, report.job_id, f"La subida falló: {public_message(exc, secrets or [])}"
        )
        raise typer.Exit(code=1) from exc
    uploader.close()
    typer.echo(f"\nGuardado en Supabase como {meta.estado}: {meta.slug}")


@app.command()
def main(
    entrada: Annotated[Path, typer.Argument(help="Vídeo (.mp4…) o texto (.txt/.md)")],
    fecha: Annotated[
        str, typer.Option("--fecha", help="Fecha del discurso o artículo (AAAA-MM-DD)")
    ],
    speakers_file: Annotated[
        Path | None,
        typer.Option(
            "--speakers",
            help='JSON con quién es cada orador: {"SPEAKER_0": {"name": …, "party": …, "role": …}}',
        ),
    ] = None,
    oradores: Annotated[
        str | None,
        typer.Option(help='Nombres de los oradores: "speaker_0=Nombre,speaker_1=Nombre"'),
    ] = None,
    autor: Annotated[str | None, typer.Option(help="Autor del texto (solo texto)")] = None,
    from_step: Annotated[
        str | None,
        typer.Option("--from-step", help=f"Recalcula desde este paso: {', '.join(STEPS)}"),
    ] = None,
    purge_audio: Annotated[
        bool, typer.Option("--purge-audio", help="Borra el audio extraído tras transcribir")
    ] = False,
    email: Annotated[
        str | None, typer.Option("--email", help="Envía el informe a esta dirección")
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="No reutiliza búsquedas guardadas (sí guarda las nuevas)"),
    ] = False,
    subir: Annotated[
        bool,
        typer.Option(
            "--subir",
            help="Guarda el informe en Supabase como borrador (visible en el panel)",
        ),
    ] = False,
    titulo: Annotated[
        str | None, typer.Option("--titulo", help="Título del evento (con --subir)")
    ] = None,
    tipo: Annotated[
        str | None,
        typer.Option(
            "--tipo", help="debate, mitin, entrevista o sesion_parlamentaria (con --subir)"
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Más detalle")] = False,
) -> None:
    """Analiza un vídeo o texto y genera data/<job_id>/report.json."""
    speech_date = _parse_date(fecha)
    speakers = _parse_speakers(oradores)
    step = _parse_step(from_step)
    speaker_info = _parse_speakers_file(speakers_file)
    if email is not None and not is_valid_email(email):
        raise typer.BadParameter("la dirección de correo no es válida", param_hint="--email")
    event_type = _parse_event_type(tipo, subir, titulo)
    _setup_logging(verbose)

    settings = get_settings()
    install_secret_filter(settings)
    uploader = _begin_upload(settings, entrada, speech_date, titulo, event_type) if subir else None
    deps = LiveDeps(settings, use_cache=not no_cache)
    try:
        result = run_job(
            entrada,
            speech_date=speech_date,
            data_dir=settings.data_dir,
            deps=deps,
            speakers=speakers,
            author=autor,
            speaker_info=speaker_info,
            from_step=step,
            purge_audio=purge_audio,
        )
    except USER_ERRORS as exc:
        if uploader is not None:
            _record_failure(
                uploader, job_id_for(entrada), public_message(exc, secret_values(settings))
            )
        typer.secho(f"\nError: {exc}", fg=typer.colors.RED, err=True)
        typer.echo(
            "Cuando lo corrijas, repite el mismo comando: se reanudará desde el paso que "
            "falló sin repetir lo ya hecho.",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except BaseException as exc:  # unexpected failure or Ctrl+C: never left "en_analisis"
        if uploader is not None:
            _record_failure(
                uploader, job_id_for(entrada), f"Análisis interrumpido: {type(exc).__name__}"
            )
        raise

    typer.echo("\n" + result.summary)
    searches = deps.searches
    typer.echo(
        f"Búsquedas Tavily en esta ejecución: {searches.real} reales "
        f"({searches.credits} créditos), {searches.cached} desde caché, "
        f"{searches.indicator_hits} desde la base de indicadores"
        + (" (caché sin leer: --no-cache)" if no_cache else "")
    )
    if uploader is not None:
        _finish_upload(
            uploader,
            settings.data_dir / result.report.job_id,
            result.report,
            secret_values(settings),
        )
    if email:
        try:
            email_report(result.report, email, settings)
        except (EmailError, ConfigError) as exc:
            typer.secho(
                f"\nEl análisis terminó, pero no se pudo enviar el correo: {exc}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from exc
        typer.echo(f"\nInforme enviado a {email}")
