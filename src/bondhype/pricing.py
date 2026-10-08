"""DeepSeek token prices (USD per 1M tokens) and the cost of one call."""

from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path

import yaml

TOKENS_PER_PRICE_UNIT = 1_000_000


class UnknownModel(Exception):
    """The pricing file has no rates for this model."""


@dataclass(frozen=True)
class Usage:
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Rates:
    cache_hit: float
    cache_miss: float
    output: float


@dataclass(frozen=True)
class ModelRates:
    off_peak: Rates
    peak: Rates


@dataclass(frozen=True)
class Pricing:
    peak_windows: tuple[tuple[time, time], ...]  # UTC, start inclusive, end exclusive
    models: dict[str, ModelRates]


def _window(text: str) -> tuple[time, time]:
    start, end = text.split("-")
    return time.fromisoformat(start), time.fromisoformat(end)


def load_pricing(path: Path) -> Pricing:
    raw = yaml.safe_load(Path(path).read_text())
    return Pricing(
        peak_windows=tuple(_window(w) for w in raw["peak_windows_utc"]),
        models={
            name: ModelRates(off_peak=Rates(**rates["off_peak"]), peak=Rates(**rates["peak"]))
            for name, rates in raw["models"].items()
        },
    )


def call_cost(usage: Usage, model: str, at: datetime, pricing: Pricing) -> float:
    if model not in pricing.models:
        raise UnknownModel(model)
    clock = at.astimezone(UTC).time()
    is_peak = any(start <= clock < end for start, end in pricing.peak_windows)
    rates = pricing.models[model].peak if is_peak else pricing.models[model].off_peak
    return (
        usage.cache_hit_tokens * rates.cache_hit
        + usage.cache_miss_tokens * rates.cache_miss
        + usage.output_tokens * rates.output
    ) / TOKENS_PER_PRICE_UNIT
