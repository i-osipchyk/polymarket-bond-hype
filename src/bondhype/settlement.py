import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime

from bondhype.arms import ARMS
from bondhype.portfolio import PositionOpened, positions_prefix
from bondhype.storage import Storage

STRATEGIES = ("bond", "hype")


@dataclass(frozen=True)
class Resolution:
    arm: str
    strategy: str
    market_id: str
    side: str
    config_version: str
    outcome: str
    payout_usd: float
    pnl_usd: float
    resolved_at: datetime
    days_overdue: float
    uma_statuses: tuple[str, ...]

    @property
    def was_disputed(self) -> bool:
        return "disputed" in self.uma_statuses

    def to_json(self) -> bytes:
        record = asdict(self) | {"resolved_at": self.resolved_at.isoformat()}
        return json.dumps(record, sort_keys=True, allow_nan=False).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "Resolution":
        record = json.loads(data)
        return cls(
            **record
            | {
                "resolved_at": datetime.fromisoformat(record["resolved_at"]),
                "uma_statuses": tuple(record["uma_statuses"]),
            }
        )


def resolution_key(arm: str, strategy: str, market_id: str) -> str:
    return f"arms/{arm}/{strategy}/resolutions/{market_id}.json"


def unsettled_positions(storage: Storage) -> Iterator[PositionOpened]:
    """Every stored position that has no resolution file yet."""
    for arm in ARMS:
        for strategy in STRATEGIES:
            for key in storage.list(positions_prefix(arm, strategy)):
                position = PositionOpened.from_json(storage.get(key))
                if not storage.exists(resolution_key(arm, strategy, position.market_id)):
                    yield position


def _final_prices(raw_market: dict) -> tuple[float, float] | None:
    """(yes, no) once the market is resolved with a clean 1/0 result, else None (fail closed)."""
    if not raw_market.get("closed") or raw_market.get("umaResolutionStatus") != "resolved":
        return None
    try:
        prices = tuple(float(p) for p in json.loads(raw_market["outcomePrices"]))
    except (KeyError, TypeError, ValueError):
        return None
    if prices not in {(1.0, 0.0), (0.0, 1.0)}:
        return None
    return prices


def _parse_closed_time(value: str | None) -> datetime | None:
    """Gamma writes closedTime as '2024-11-06 18:03:54+00'."""
    try:
        return datetime.fromisoformat(value.replace(" ", "T", 1) + ":00")
    except (AttributeError, ValueError):
        return None


def _uma_statuses(raw_market: dict) -> tuple[str, ...]:
    return tuple(json.loads(raw_market.get("umaResolutionStatuses") or "[]"))


def resolve(position: PositionOpened, raw_market: dict) -> Resolution | None:
    prices = _final_prices(raw_market)
    if prices is None:
        return None
    yes_price, no_price = prices
    won_price = no_price if position.side == "NO" else yes_price
    payout = position.shares * won_price
    resolved_at = _parse_closed_time(raw_market.get("closedTime"))
    if resolved_at is None:
        return None
    end_date = datetime.fromisoformat(raw_market["endDate"])
    return Resolution(
        arm=position.arm,
        strategy=position.strategy,
        market_id=position.market_id,
        side=position.side,
        config_version=position.config_version,
        outcome="win" if payout > 0 else "loss",
        payout_usd=payout,
        pnl_usd=payout - position.filled_usd - position.fee_usd,
        resolved_at=resolved_at,
        days_overdue=max(0.0, (resolved_at - end_date).total_seconds() / 86400),
        uma_statuses=_uma_statuses(raw_market),
    )


@dataclass(frozen=True)
class OverdueReport:
    arm: str
    strategy: str
    market_id: str
    config_version: str
    days_overdue: float
    closed: bool
    uma_status: str | None
    uma_statuses: tuple[str, ...]
    held_side_price: float
    conservative_pnl_usd: float

    @property
    def was_disputed(self) -> bool:
        return "disputed" in self.uma_statuses

    def to_json(self) -> bytes:
        record = asdict(self) | {"was_disputed": self.was_disputed}
        return json.dumps(record, sort_keys=True, allow_nan=False).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "OverdueReport":
        record = json.loads(data)
        record.pop("was_disputed")  # derived from uma_statuses
        return cls(**record | {"uma_statuses": tuple(record["uma_statuses"])})


def assess_overdue(
    position: PositionOpened, raw_market: dict, now: datetime
) -> OverdueReport | None:
    """Status of a position still unsettled after its end date; None if not overdue or settled."""
    end_date = datetime.fromisoformat(raw_market["endDate"])
    if now <= end_date or resolve(position, raw_market) is not None:
        return None
    yes_price, no_price = (float(p) for p in json.loads(raw_market["outcomePrices"]))
    return OverdueReport(
        arm=position.arm,
        strategy=position.strategy,
        market_id=position.market_id,
        config_version=position.config_version,
        days_overdue=(now - end_date).total_seconds() / 86400,
        closed=bool(raw_market.get("closed")),
        uma_status=raw_market.get("umaResolutionStatus"),
        uma_statuses=_uma_statuses(raw_market),
        held_side_price=no_price if position.side == "NO" else yes_price,
        conservative_pnl_usd=-(position.filled_usd + position.fee_usd),
    )
