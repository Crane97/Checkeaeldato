"""Estimación de coste por llamada a partir de pricing.toml."""

import logging
import tomllib
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel

from pipeline.models import CostEntry

logger = logging.getLogger(__name__)


class TokenPrice(BaseModel):
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float


class AudioPrice(BaseModel):
    usd_per_hour: float


class SearchPrice(BaseModel):
    usd_per_credit: float


class Pricing(BaseModel):
    anthropic: dict[str, TokenPrice]
    transcription: dict[str, AudioPrice]
    search: dict[str, SearchPrice] = {}


def load_pricing(path: Path | None = None) -> Pricing:
    text = (
        path.read_text(encoding="utf-8")
        if path
        else files("pipeline").joinpath("pricing.toml").read_text(encoding="utf-8")
    )
    return Pricing.model_validate(tomllib.loads(text))


@lru_cache
def default_pricing() -> Pricing:
    return load_pricing()


def llm_cost(
    step: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    pricing: Pricing | None = None,
) -> CostEntry:
    price = (pricing or default_pricing()).anthropic.get(model)
    priced = True
    if price is None:
        logger.warning("Sin precio para el modelo %s; se registra coste 0", model)
        usd, priced = 0.0, False
    else:
        usd = (
            input_tokens * price.input_per_mtok
            + output_tokens * price.output_per_mtok
            + cache_write_tokens * price.cache_write_per_mtok
            + cache_read_tokens * price.cache_read_per_mtok
        ) / 1_000_000
    return CostEntry(
        step=step,
        provider="anthropic",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
        usd=usd,
        priced=priced,
    )


def transcription_cost(
    step: str, provider: str, audio_seconds: float, pricing: Pricing | None = None
) -> CostEntry:
    price = (pricing or default_pricing()).transcription.get(provider)
    priced = True
    if price is None:
        logger.warning("Sin precio para el proveedor %s; se registra coste 0", provider)
        usd, priced = 0.0, False
    else:
        usd = audio_seconds / 3600 * price.usd_per_hour
    return CostEntry(
        step=step, provider=provider, audio_seconds=audio_seconds, usd=usd, priced=priced
    )


def search_cost(
    step: str,
    *,
    real: int,
    cached: int,
    credits: int,
    usd_per_credit: float | None = None,
    pricing: Pricing | None = None,
) -> CostEntry:
    """Tavily searches of one step; cached ones are free."""
    if usd_per_credit is None:
        price = (pricing or default_pricing()).search.get("tavily")
        usd_per_credit = price.usd_per_credit if price else 0.0
    return CostEntry(
        step=step,
        provider="tavily",
        searches=real,
        cached_searches=cached,
        credits=credits,
        usd=credits * usd_per_credit,
    )
