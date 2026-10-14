"""Guards for every test: no real API keys, no real network, no real Claude Code.

A test that reaches Tavily, Anthropic, Google or ElevenLabs without a mock would spend
credits (and depend on the network), so it fails with RealNetworkInTest instead. Mocks
(respx, monkeypatched clients) sit above these guards and keep working.
"""

import os
import socket
import subprocess
from pathlib import Path

import psycopg
import pytest

from pipeline.config import Settings, get_settings

REPO = Path(__file__).resolve().parent.parent

SECRET_VARS = (
    "TAVILY_API_KEY",
    "GOOGLE_FACTCHECK_API_KEY",
    "ANTHROPIC_API_KEY",
    "TRANSCRIPTION_API_KEY",
    "SMTP_PASSWORD",
    "SUPABASE_DB_URL",
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}


class RealNetworkInTest(BaseException):
    """A test tried to use a real external service. BaseException on purpose: the
    pipeline's `except Exception` fallbacks must not swallow it."""


def _host_of(address: object) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECRET_VARS:
        monkeypatch.delenv(name, raising=False)
    # Settings must not pick up the developer's real .env.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_real_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    # Integration tests marked `supabase` may resolve the dev project's host, nothing else.
    supabase = request.node.get_closest_marker("supabase") is not None

    def allowed(host: str) -> bool:
        return host in _LOCAL_HOSTS or (
            supabase and host.endswith((".supabase.co", ".supabase.com"))
        )

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is not None and not allowed(str(host)):
            raise RealNetworkInTest(f"petición sin mock a {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and _host_of(address) not in (
            _LOCAL_HOSTS
        ):
            raise RealNetworkInTest(f"conexión sin mock a {_host_of(address)}")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _no_real_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = subprocess.run

    def guarded_run(cmd, *args, **kwargs):
        program = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else str(cmd).split()[0]
        if Path(os.fspath(program)).name == "claude":
            raise RealNetworkInTest("se intentó lanzar claude -p sin mock")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)


@pytest.fixture(autouse=True)
def _no_real_database(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """psycopg connects through libpq (C), which the socket guard above cannot see: block it
    unless the test is marked `supabase` (integration tests against the dev project)."""
    if request.node.get_closest_marker("supabase"):
        return

    def blocked(*args, **kwargs):
        raise RealNetworkInTest("conexión a Postgres sin el marcador @pytest.mark.supabase")

    monkeypatch.setattr(psycopg, "connect", blocked)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(lambda cls, *a, **k: blocked()))


@pytest.fixture
def supabase_url() -> str:
    """Connection string of the DEVELOPMENT project, read from the real .env; the test is
    skipped when it is not configured."""
    settings = Settings(_env_file=REPO / ".env")
    if settings.supabase_db_url is None:
        pytest.skip("SUPABASE_DB_URL no está configurada")
    return settings.supabase_db_url.get_secret_value()
