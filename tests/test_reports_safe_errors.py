"""Mensajes de error que van a la base y al registro: nunca con secretos."""

import logging

from pydantic import SecretStr

from pipeline.config import ConfigError, Settings
from pipeline.llm import LLMError
from pipeline.reports.safe_errors import (
    MASK,
    SecretFilter,
    mask,
    public_message,
    secret_values,
)

DB_URL = "postgresql://postgres.ref:contrasena-secreta@aws-0-eu.pooler.supabase.com:5432/postgres"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        supabase_db_url=SecretStr(DB_URL),
        supabase_service_role_key=SecretStr("clave-service-role"),
        tavily_api_key=SecretStr("tvly-clave"),
    )


def test_every_secret_and_the_database_password_are_known():
    values = secret_values(_settings())
    assert {"clave-service-role", "tvly-clave", "contrasena-secreta", DB_URL} <= set(values)
    assert mask(f"fallo en {DB_URL}", values) == f"fallo en {MASK}"


def test_pipeline_errors_keep_their_message_masked():
    secrets = secret_values(_settings())
    assert public_message(LLMError("Se alcanzó el límite de uso")) == "Se alcanzó el límite de uso"
    message = public_message(ConfigError("clave tvly-clave no válida"), secrets)
    assert "tvly-clave" not in message and MASK in message


def test_other_errors_only_show_their_type():
    message = public_message(RuntimeError(f"connection to {DB_URL} failed"))
    assert message.startswith("Error interno (RuntimeError)") and "pooler" not in message


def test_the_log_filter_masks_messages_and_tracebacks(caplog):
    logger = logging.getLogger("prueba_secretos")
    caplog.handler.addFilter(SecretFilter(secret_values(_settings())))
    try:
        raise OSError(f"no se pudo conectar a {DB_URL}")
    except OSError:
        logger.exception("Fallo con clave %s", "clave-service-role")
    text = caplog.text
    assert "contrasena-secreta" not in text and "clave-service-role" not in text
    assert MASK in text and "OSError" in text


def test_the_filter_never_breaks_a_badly_formatted_log_call(caplog):
    logger = logging.getLogger("prueba_formato")
    caplog.handler.addFilter(SecretFilter(["clave-service-role"]))
    logger.warning("dos huecos %s %s", "clave-service-role")  # one argument missing
    assert "clave-service-role" not in caplog.text


def test_a_percent_encoded_password_is_also_known():
    settings = Settings(
        _env_file=None, supabase_db_url=SecretStr("postgresql://u:p%40ss-w0rd@h/db")
    )
    assert {"p%40ss-w0rd", "p@ss-w0rd"} <= set(secret_values(settings))
