"""Entrada de línea de comandos: `uv run verificar <video|texto> --fecha AAAA-MM-DD`."""

import datetime as dt
import logging
from pathlib import Path
from typing import Annotated, cast

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
from pipeline.run import LiveDeps, run_job
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
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Más detalle")] = False,
) -> None:
    """Analiza un vídeo o texto y genera data/<job_id>/report.json."""
    speech_date = _parse_date(fecha)
    speakers = _parse_speakers(oradores)
    step = _parse_step(from_step)
    speaker_info = _parse_speakers_file(speakers_file)
    if email is not None and not is_valid_email(email):
        raise typer.BadParameter("la dirección de correo no es válida", param_hint="--email")
    _setup_logging(verbose)

    settings = get_settings()
    try:
        result = run_job(
            entrada,
            speech_date=speech_date,
            data_dir=settings.data_dir,
            deps=LiveDeps(settings),
            speakers=speakers,
            author=autor,
            speaker_info=speaker_info,
            from_step=step,
            purge_audio=purge_audio,
        )
    except USER_ERRORS as exc:
        typer.secho(f"\nError: {exc}", fg=typer.colors.RED, err=True)
        typer.echo(
            "Cuando lo corrijas, repite el mismo comando: se reanudará desde el paso que "
            "falló sin repetir lo ya hecho.",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo("\n" + result.summary)
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
