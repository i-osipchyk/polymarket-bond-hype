import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from bondhype.config import Config
from bondhype.storage import Storage

if TYPE_CHECKING:
    from bondhype.settlement import Resolution


@dataclass(frozen=True)
class PositionOpened:
    arm: str
    strategy: str
    market_id: str
    event_id: str
    side: str
    opened_at: datetime
    config_version: str
    shares: float
    filled_usd: float
    avg_price: float
    best_ask: float
    slippage_per_share: float
    gross_edge_per_share: float
    fee_usd: float
    partial: bool

    def to_json(self) -> bytes:
        record = asdict(self) | {"opened_at": self.opened_at.isoformat()}
        return json.dumps(record, sort_keys=True, allow_nan=False).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "PositionOpened":
        record = json.loads(data)
        return cls(**record | {"opened_at": datetime.fromisoformat(record["opened_at"])})


def positions_prefix(arm: str, strategy: str) -> str:
    return f"arms/{arm}/{strategy}/positions/"


def position_key(arm: str, strategy: str, market_id: str) -> str:
    return f"{positions_prefix(arm, strategy)}{market_id}.json"


@dataclass(frozen=True)
class Portfolio:
    balance_usd: float
    exposure_usd: float
    open_positions_by_event: dict[str, int]
    open_market_ids: set[str]

    @property
    def bankroll_usd(self) -> float:
        return self.balance_usd + self.exposure_usd

    @classmethod
    def from_events(
        cls,
        events: list[PositionOpened],
        starting_balance_usd: float,
        resolutions: "list[Resolution] | None" = None,
    ) -> "Portfolio":
        payouts = {r.market_id: r.payout_usd for r in resolutions or []}
        open_events = [e for e in events if e.market_id not in payouts]
        return cls(
            balance_usd=starting_balance_usd
            - sum(e.filled_usd + e.fee_usd for e in events)
            + sum(payouts.values()),
            exposure_usd=sum(e.filled_usd for e in open_events),
            open_positions_by_event=dict(Counter(e.event_id for e in open_events)),
            open_market_ids={e.market_id for e in open_events},
        )


def resolutions_prefix(arm: str, strategy: str) -> str:
    return f"arms/{arm}/{strategy}/resolutions/"


def load_portfolio(storage: Storage, config: Config, arm: str, strategy: str) -> Portfolio:
    from bondhype.settlement import Resolution  # settlement imports this module

    events = [
        PositionOpened.from_json(storage.get(key))
        for key in storage.list(positions_prefix(arm, strategy))
    ]
    resolutions = [
        Resolution.from_json(storage.get(key))
        for key in storage.list(resolutions_prefix(arm, strategy))
    ]
    return Portfolio.from_events(events, config.portfolio.starting_balance_usd, resolutions)
