from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid."""


Price = Annotated[float, Field(ge=0, le=1)]


class BondFilters(_Strict):
    price_min: Price
    price_max: Price
    days_to_resolution_min: float
    days_to_resolution_max: float
    liquidity_multiple: float
    max_spread: float
    min_total_volume_usd: float
    min_annualised_yield: float

    @model_validator(mode="after")
    def _band_is_ordered(self):
        if self.price_min > self.price_max:
            raise ValueError("price_min must not exceed price_max")
        return self


class HypeFilters(_Strict):
    yes_price_min: Price
    yes_price_max: Price
    days_to_resolution_min: float
    days_to_resolution_max: float
    liquidity_multiple: float
    max_spread: float
    min_total_volume_usd: float

    @model_validator(mode="after")
    def _band_is_ordered(self):
        if self.yes_price_min > self.yes_price_max:
            raise ValueError("yes_price_min must not exceed yes_price_max")
        return self


class PortfolioRules(_Strict):
    starting_balance_usd: float = Field(gt=0)
    max_positions_per_event: int = Field(gt=0)
    max_deployed_fraction: float = Field(gt=0, le=1)
    cooldown_hours: float = Field(ge=0)
    cooldown_price_move: float = Field(ge=0, le=1)


class Config(_Strict):
    version: str
    order_size_usd: float
    prescreen_price_margin: float
    portfolio: PortfolioRules
    bond: BondFilters
    hype: HypeFilters


def load_config(path: Path) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text())
        return Config.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid config {path}: {exc}") from exc
