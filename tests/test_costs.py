import pytest

from pipeline.costs import default_pricing, llm_cost, transcription_cost


def test_bundled_pricing_loads_and_covers_configured_provider():
    pricing = default_pricing()
    assert "elevenlabs" in pricing.transcription
    assert "claude-opus-5-5" in pricing.anthropic


def test_llm_cost_combines_all_token_kinds():
    entry = llm_cost(
        "claims",
        "claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_write_tokens=200_000,
        cache_read_tokens=500_000,
    )
    # 4.00 + 2.00 + 1.00 + 0.10
    assert entry.usd == pytest.approx(7.10)
    assert entry.provider == "anthropic"
    assert entry.cache_read_tokens == 500_000


def test_unknown_model_costs_zero_and_warns(caplog):
    entry = llm_cost("claims", "modelo-inexistente", input_tokens=1000, output_tokens=10)
    assert entry.usd == 0
    assert entry.priced is False
    assert llm_cost("claims", "claude-opus-5-5", input_tokens=1, output_tokens=1).priced
    assert "modelo-inexistente" in caplog.text


def test_transcription_cost_per_hour():
    entry = transcription_cost("transcript", "elevenlabs", audio_seconds=7200)
    assert entry.usd == pytest.approx(0.44)
    assert entry.audio_seconds == 7200
