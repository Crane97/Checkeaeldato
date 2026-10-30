"""Mensajes de error sin secretos, para la base (visibles en el panel) y para el registro.

Un error de la pipeline (clave inválida, límite de uso, entrada no válida…) lleva un mensaje
pensado para el usuario y se guarda tal cual. Cualquier otro (un fallo de psycopg o httpx)
puede incluir la URL de la base con su contraseña o fragmentos de una respuesta: en la base
solo queda su tipo y el detalle va al registro, con los secretos tapados.
"""

import logging
from collections.abc import Iterable
from urllib.parse import unquote, urlsplit

from pydantic import SecretStr

from pipeline.audio import AudioError
from pipeline.claims import ClaimsError
from pipeline.config import ConfigError, Settings
from pipeline.evidence.base import EvidenceError
from pipeline.jobs import JobError
from pipeline.llm import LLMError
from pipeline.promises import PromiseError
from pipeline.report import ReportError
from pipeline.reports.files import StorageError
from pipeline.reports.repository import ReportError as StoredReportError
from pipeline.text_input import InputError
from pipeline.transcribe import TranscriptionError
from pipeline.verdict import VerdictError

MASK = "***"
# Errors whose message is written for the user (they never carry a key).
USER_FACING = (
    ConfigError, JobError, InputError, AudioError, TranscriptionError, LLMError, ClaimsError,
    EvidenceError, VerdictError, PromiseError, ReportError, StoredReportError, StorageError,
)  # fmt: skip
MAX_MESSAGE = 500


def secret_values(settings: Settings) -> list[str]:
    """Every secret in the settings, plus the password inside SUPABASE_DB_URL."""
    values = []
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr) and value.get_secret_value():
            values.append(value.get_secret_value())
    if settings.supabase_db_url is not None:
        password = urlsplit(settings.supabase_db_url.get_secret_value()).password
        if password:
            values += [password, unquote(password)]  # an error may show it decoded
    # Longest first, so a password inside a URL is not left half covered.
    return sorted({v for v in values if len(v) >= 4}, key=len, reverse=True)


def mask(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, MASK)
    return text


def public_message(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """What the report's `error` column shows."""
    if isinstance(exc, USER_FACING) and str(exc).strip():
        return mask(str(exc), secrets)[:MAX_MESSAGE]
    return f"Error interno ({type(exc).__name__}): el detalle está en el registro del servidor"


class SecretFilter(logging.Filter):
    """Covers secrets in log messages and tracebacks (install it on the handlers)."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = list(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — bad format args: never break the caller's log call
            message = f"{record.msg} {record.args}"
        record.msg = mask(message, self._secrets)
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask(record.exc_text, self._secrets)
        if record.stack_info:
            record.stack_info = mask(record.stack_info, self._secrets)
        return True


def install_secret_filter(settings: Settings) -> None:
    """Add the filter to every handler there is now: the root's and those of loggers that do
    not propagate (uvicorn's). Call it after configuring logging, also when the app starts."""
    secret_filter = SecretFilter(secret_values(settings))
    loggers = [logging.getLogger()] + [
        lg for lg in logging.Logger.manager.loggerDict.values() if isinstance(lg, logging.Logger)
    ]
    for lg in loggers:
        for handler in lg.handlers:
            if secret_filter not in handler.filters:
                handler.addFilter(secret_filter)
