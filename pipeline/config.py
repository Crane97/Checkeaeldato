"""Carga de variables de entorno (.env)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LlmStep = Literal["detection", "verdict"]


class ConfigError(RuntimeError):
    """Missing or invalid configuration; the message is shown to the user."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # "api": Anthropic API (needed for the public beta). "claude-code": the local Claude
    # Code subscription via `claude -p`, for local testing without prepaid credits.
    llm_backend: Literal["api", "claude-code"] = "api"

    # Optional: the Anthropic SDK can also resolve credentials on its own.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str | None = None
    anthropic_model_detection: str | None = None
    anthropic_model_verdict: str | None = None

    transcription_provider: Literal["elevenlabs"] = "elevenlabs"
    transcription_api_key: SecretStr | None = None

    # Claude's internal thinking in the verdict step only. Unset: the model's default
    # (Claude 5.5 models already think adaptively). Effort steers depth on Claude 4.6+;
    # VERDICT_THINKING_BUDGET (tokens) is only accepted by models up to Claude 4.5.
    verdict_thinking: bool | None = None
    verdict_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    verdict_thinking_budget: int | None = Field(default=None, ge=1024)

    google_factcheck_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None
    # "basic" costs 1 credit per search, "advanced" 2.
    tavily_search_depth: Literal["basic", "advanced"] = "basic"
    tavily_max_results: int = Field(default=5, ge=1, le=20)
    # Searches older than this are repeated (statistics get updated).
    tavily_cache_days: int = Field(default=30, ge=0)

    # Sending reports by email (any SMTP provider; Gmail needs an app password).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    email_from: str | None = None  # defaults to SMTP_USER

    data_dir: Path = Path("data")

    def model_for(self, step: LlmStep) -> str:
        override = {
            "detection": self.anthropic_model_detection,
            "verdict": self.anthropic_model_verdict,
        }[step]
        model = override or self.anthropic_model
        if not model:
            raise ConfigError(
                f"No hay modelo de Claude para el paso '{step}': define ANTHROPIC_MODEL "
                f"o ANTHROPIC_MODEL_{step.upper()} en .env"
            )
        return model

    def secret(self, field: str) -> str:
        value: SecretStr | None = getattr(self, field)
        if value is None:
            raise ConfigError(f"Falta {field.upper()} en .env")
        return value.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
