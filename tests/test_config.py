import pytest

from pipeline.config import ConfigError, Settings

ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_MODEL_DETECTION",
    "ANTHROPIC_MODEL_VERDICT",
    "TRANSCRIPTION_PROVIDER",
    "TRANSCRIPTION_API_KEY",
    "GOOGLE_FACTCHECK_API_KEY",
    "TAVILY_API_KEY",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def make_settings(**env) -> Settings:
    # _env_file=None keeps a developer's real .env out of the tests.
    return Settings(_env_file=None, **env)


def test_step_model_falls_back_to_default():
    settings = make_settings(anthropic_model="claude-opus-5-5")
    assert settings.model_for("detection") == "claude-opus-5-5"
    assert settings.model_for("verdict") == "claude-opus-5-5"


def test_step_model_override_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("ANTHROPIC_MODEL_DETECTION", "claude-sonnet-5-5")
    settings = make_settings()
    assert settings.model_for("detection") == "claude-sonnet-5-5"
    assert settings.model_for("verdict") == "claude-opus-5-5"


def test_empty_env_values_count_as_unset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("ANTHROPIC_MODEL_VERDICT", "")
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "")
    settings = make_settings()
    assert settings.model_for("verdict") == "claude-opus-5-5"
    assert settings.transcription_provider == "elevenlabs"


def test_missing_model_raises_config_error():
    with pytest.raises(ConfigError, match="ANTHROPIC_MODEL"):
        make_settings().model_for("verdict")


def test_secret_returns_value_or_raises(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_API_KEY", "xi-123")
    settings = make_settings()
    assert settings.secret("transcription_api_key") == "xi-123"
    with pytest.raises(ConfigError, match="GOOGLE_FACTCHECK_API_KEY"):
        settings.secret("google_factcheck_api_key")


def test_secrets_are_not_printed():
    settings = make_settings(transcription_api_key="xi-123")
    assert "xi-123" not in repr(settings)
