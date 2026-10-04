from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    """Bids and asks as given; no ordering is assumed by consumers."""

    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


@dataclass(frozen=True)
class FeeSchedule:
    rate: float
    exponent: float
    enabled: bool


@dataclass(frozen=True)
class Market:
    id: str
    question: str
    end_date: datetime
    volume_usd: float
    yes_price: float
    no_price: float
    event_id: str
    yes_token: str = ""
    no_token: str = ""
    outcomes: tuple[str, str] = ("Yes", "No")
    fee_schedule: FeeSchedule | None = None
