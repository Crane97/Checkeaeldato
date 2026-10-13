"""The test suite must never call a real external API (and spend credits)."""

import subprocess

import httpx
import pytest
import respx

from pipeline.config import Settings
from pipeline.evidence.tavily import API_URL, TavilyClient
from tests.conftest import RealNetworkInTest


def test_unmocked_tavily_search_fails_the_test():
    client = TavilyClient("tvly-key-que-no-deberia-usarse", backoff=0, max_attempts=1)
    with pytest.raises(RealNetworkInTest, match="api.tavily.com"):
        client.search("tasa de paro")


def test_any_unmocked_external_http_fails():
    with pytest.raises(RealNetworkInTest, match="api.anthropic.com"):
        httpx.get("https://api.anthropic.com/v1/models")


@respx.mock
def test_mocked_tavily_still_works():
    respx.post(API_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    assert TavilyClient("k", backoff=0).search("paro").results == []


def test_keys_are_not_read_from_dotenv_or_environment():
    settings = Settings()
    assert settings.tavily_api_key is None
    assert settings.google_factcheck_api_key is None
    assert settings.anthropic_api_key is None


def test_launching_claude_code_fails():
    with pytest.raises(RealNetworkInTest, match="claude"):
        subprocess.run(["claude", "-p", "hola"], check=False)
