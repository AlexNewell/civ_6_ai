"""Model prices, and what a call costs or could cost at most."""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Literal

CacheTtl = Literal["5m", "1h"]

_PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Usage:
    """Billed tokens for one API response. `input_tokens` excludes cached tokens."""

    input_tokens: int
    output_tokens: int
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0

    def __post_init__(self) -> None:
        if min(vars(self).values()) < 0:
            raise ValueError(f"negative token count: {self}")

    @classmethod
    def from_api(cls, usage: Mapping) -> "Usage":
        """Read the `usage` object of a Messages API response, as a dict.

        If a response doesn't split cache writes by TTL, they count at the dearer 1-hour rate.
        """
        written = usage.get("cache_creation_input_tokens") or 0
        split = usage.get("cache_creation")
        return cls(
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_write_5m_tokens=split["ephemeral_5m_input_tokens"] if split else 0,
            cache_write_1h_tokens=split["ephemeral_1h_input_tokens"] if split else written,
            cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
        )

    @property
    def prompt_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.cache_read_tokens
        )


@dataclass(frozen=True)
class Rates:
    """USD per million tokens."""

    input: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal
    output: Decimal

    @classmethod
    def parse(cls, raw: Mapping) -> "Rates":
        return cls(**{field: Decimal(raw[field]) for field in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ModelPrice:
    rates: Rates
    long_prompt_threshold: int | None = None
    long_prompt_rates: Rates | None = None

    def rates_for(self, prompt_tokens: int) -> Rates:
        if self.long_prompt_rates and prompt_tokens > self.long_prompt_threshold:
            return self.long_prompt_rates
        return self.rates


@dataclass(frozen=True)
class Pricing:
    cap_usd: Decimal
    models: Mapping[str, ModelPrice]

    def cost(self, model: str, usage: Usage) -> Decimal:
        rates = self.models[model].rates_for(usage.prompt_tokens)
        return (
            usage.input_tokens * rates.input
            + usage.cache_write_5m_tokens * rates.cache_write_5m
            + usage.cache_write_1h_tokens * rates.cache_write_1h
            + usage.cache_read_tokens * rates.cache_read
            + usage.output_tokens * rates.output
        ) / _PER_MILLION

    def worst_case(
        self,
        model: str,
        max_input_tokens: int,
        max_output_tokens: int,
        cache_write_ttl: CacheTtl | None,
    ) -> Decimal:
        """Highest possible cost, with every input token billed at its dearest rate."""
        if max_input_tokens < 0 or max_output_tokens < 1:
            raise ValueError(f"invalid token limits: {max_input_tokens}, {max_output_tokens}")
        rates = self.models[model].rates_for(max_input_tokens)
        input_rate = {
            None: rates.input,
            "5m": rates.cache_write_5m,
            "1h": rates.cache_write_1h,
        }[cache_write_ttl]
        return (max_input_tokens * input_rate + max_output_tokens * rates.output) / _PER_MILLION


def load_pricing(path: Path | None = None) -> Pricing:
    """Load prices from `path`, or from the file shipped with this package."""
    source = path or resources.files("civ6_ai.budget").joinpath("pricing.toml")
    raw = tomllib.loads(source.read_text(encoding="utf-8"))
    models = {}
    for name, entry in raw["models"].items():
        long_prompt = entry.get("long_prompt")
        models[name] = ModelPrice(
            rates=Rates.parse(entry),
            long_prompt_threshold=entry.get("long_prompt_threshold"),
            long_prompt_rates=Rates.parse(long_prompt) if long_prompt else None,
        )
    return Pricing(cap_usd=Decimal(raw["budget"]["cap_usd_per_player_per_game"]), models=models)
