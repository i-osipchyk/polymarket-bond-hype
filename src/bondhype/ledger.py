"""Every trade in one table, `state/trades.csv`, read once and written at most once per run.

One row per arm, strategy and market: the paper fill, a status, the resolution once there is one,
and the latest overdue assessment. The scanner adds rows, the tracker resolves them and the
overdue job annotates them. Writes are conditional on the version read, so two jobs that overlap
cannot silently drop each other's changes: the later one fails with `StaleWrite` and is retried.
"""

from dataclasses import dataclass, field
from datetime import date

from bondhype import csvtable as c
from bondhype.arms import ARMS
from bondhype.config import Config
from bondhype.portfolio import Portfolio, PositionOpened, positions_prefix
from bondhype.settlement import STRATEGIES, OverdueReport, Resolution
from bondhype.storage import Storage

TRADES_KEY = "state/trades.csv"

COLUMNS = (
    "arm",
    "strategy",
    "market_id",
    "status",
    "event_id",
    "side",
    "opened_at",
    "config_version",
    "shares",
    "filled_usd",
    "avg_price",
    "best_ask",
    "slippage_per_share",
    "gross_edge_per_share",
    "fee_usd",
    "partial",
    "resolved_at",
    "outcome",
    "payout_usd",
    "pnl_usd",
    "resolved_days_overdue",
    "resolved_uma_statuses",
    "overdue_checked_on",
    "overdue_days",
    "overdue_closed",
    "overdue_uma_status",
    "overdue_uma_statuses",
    "overdue_held_side_price",
    "overdue_conservative_pnl_usd",
)

TradeKey = tuple[str, str, str]  # arm, strategy, market id


class MissingTradesTable(Exception):
    """trades.csv is absent while per-record files exist: run `bondhype.migrate` first."""


@dataclass
class Trade:
    position: PositionOpened
    resolution: Resolution | None = None
    overdue: OverdueReport | None = None
    overdue_checked_on: date | None = None

    @property
    def status(self) -> str:
        if self.resolution is not None:
            return "resolved"
        return "overdue" if self.overdue is not None else "open"


def _key(arm: str, strategy: str, market_id: str) -> TradeKey:
    return (arm, strategy, market_id)


@dataclass
class Ledger:
    trades: dict[TradeKey, Trade] = field(default_factory=dict)
    version: str | None = None  # the stored version this was read at; None: no table yet
    changed: bool = False

    def _trades(self, arm: str, strategy: str) -> list[Trade]:
        return [t for (a, s, _), t in sorted(self.trades.items()) if (a, s) == (arm, strategy)]

    def positions(self, arm: str, strategy: str) -> list[PositionOpened]:
        return [t.position for t in self._trades(arm, strategy)]

    def resolutions(self, arm: str, strategy: str) -> list[Resolution]:
        return [t.resolution for t in self._trades(arm, strategy) if t.resolution is not None]

    def overdue(self, arm: str, strategy: str) -> list[OverdueReport]:
        """Latest overdue assessment of each still-unresolved position."""
        return [
            t.overdue
            for t in self._trades(arm, strategy)
            if t.overdue is not None and t.resolution is None
        ]

    def holds(self, arm: str, strategy: str, market_id: str) -> bool:
        return _key(arm, strategy, market_id) in self.trades

    def is_settled(self, position: PositionOpened) -> bool:
        trade = self.trades.get(_key(position.arm, position.strategy, position.market_id))
        return trade is not None and trade.resolution is not None

    def unsettled(self) -> list[PositionOpened]:
        """Open positions in arm, then strategy, then market order."""
        return [
            p
            for arm in ARMS
            for strategy in STRATEGIES
            for p in self.positions(arm, strategy)
            if not self.is_settled(p)
        ]

    def portfolio(self, config: Config, arm: str, strategy: str) -> Portfolio:
        return Portfolio.from_events(
            self.positions(arm, strategy),
            config.portfolio.starting_balance_usd,
            self.resolutions(arm, strategy),
        )

    def add_position(self, position: PositionOpened) -> None:
        self.trades[_key(position.arm, position.strategy, position.market_id)] = Trade(position)
        self.changed = True

    def add_resolution(self, resolution: Resolution) -> None:
        self.trades[
            _key(resolution.arm, resolution.strategy, resolution.market_id)
        ].resolution = resolution
        self.changed = True

    def set_overdue(self, report: OverdueReport, checked_on: date) -> None:
        trade = self.trades[_key(report.arm, report.strategy, report.market_id)]
        trade.overdue, trade.overdue_checked_on = report, checked_on
        self.changed = True


def _row(trade: Trade) -> dict[str, str]:
    p, r, o = trade.position, trade.resolution, trade.overdue
    return {
        "arm": p.arm,
        "strategy": p.strategy,
        "market_id": p.market_id,
        "status": trade.status,
        "event_id": p.event_id,
        "side": p.side,
        "opened_at": c.moment(p.opened_at),
        "config_version": p.config_version,
        "shares": c.number(p.shares),
        "filled_usd": c.number(p.filled_usd),
        "avg_price": c.number(p.avg_price),
        "best_ask": c.number(p.best_ask),
        "slippage_per_share": c.number(p.slippage_per_share),
        "gross_edge_per_share": c.number(p.gross_edge_per_share),
        "fee_usd": c.number(p.fee_usd),
        "partial": c.flag(p.partial),
        "resolved_at": c.moment(r.resolved_at if r else None),
        "outcome": c.text(r.outcome if r else None),
        "payout_usd": c.number(r.payout_usd if r else None),
        "pnl_usd": c.number(r.pnl_usd if r else None),
        "resolved_days_overdue": c.number(r.days_overdue if r else None),
        "resolved_uma_statuses": c.items(r.uma_statuses if r else None),
        "overdue_checked_on": c.moment(trade.overdue_checked_on if o else None),
        "overdue_days": c.number(o.days_overdue if o else None),
        "overdue_closed": c.flag(o.closed if o else None),
        "overdue_uma_status": c.text(o.uma_status if o else None),
        "overdue_uma_statuses": c.items(o.uma_statuses if o else None),
        "overdue_held_side_price": c.number(o.held_side_price if o else None),
        "overdue_conservative_pnl_usd": c.number(o.conservative_pnl_usd if o else None),
    }


def _trade(row: dict[str, str]) -> Trade:
    position = PositionOpened(
        arm=row["arm"],
        strategy=row["strategy"],
        market_id=row["market_id"],
        event_id=row["event_id"],
        side=row["side"],
        opened_at=c.to_datetime(row["opened_at"]),
        config_version=row["config_version"],
        shares=c.to_float(row["shares"]),
        filled_usd=c.to_float(row["filled_usd"]),
        avg_price=c.to_float(row["avg_price"]),
        best_ask=c.to_float(row["best_ask"]),
        slippage_per_share=c.to_float(row["slippage_per_share"]),
        gross_edge_per_share=c.to_float(row["gross_edge_per_share"]),
        fee_usd=c.to_float(row["fee_usd"]),
        partial=c.to_bool(row["partial"]),
    )
    resolution = None
    if row["resolved_at"]:
        resolution = Resolution(
            arm=position.arm,
            strategy=position.strategy,
            market_id=position.market_id,
            side=position.side,
            config_version=position.config_version,
            outcome=row["outcome"],
            payout_usd=c.to_float(row["payout_usd"]),
            pnl_usd=c.to_float(row["pnl_usd"]),
            resolved_at=c.to_datetime(row["resolved_at"]),
            days_overdue=c.to_float(row["resolved_days_overdue"]),
            uma_statuses=c.to_items(row["resolved_uma_statuses"]),
        )
    overdue, checked_on = None, None
    if row["overdue_checked_on"]:
        checked_on = date.fromisoformat(row["overdue_checked_on"])
        overdue = OverdueReport(
            arm=position.arm,
            strategy=position.strategy,
            market_id=position.market_id,
            config_version=position.config_version,
            days_overdue=c.to_float(row["overdue_days"]),
            closed=c.to_bool(row["overdue_closed"]),
            uma_status=c.opt_text(row["overdue_uma_status"]),
            uma_statuses=c.to_items(row["overdue_uma_statuses"]),
            held_side_price=c.to_float(row["overdue_held_side_price"]),
            conservative_pnl_usd=c.to_float(row["overdue_conservative_pnl_usd"]),
        )
    return Trade(position, resolution, overdue, checked_on)


def encode_ledger(ledger: Ledger) -> bytes:
    return c.encode(COLUMNS, (_row(t) for _, t in sorted(ledger.trades.items())))


def decode_ledger(data: bytes, version: str | None = None) -> Ledger:
    trades = [_trade(row) for row in c.decode(COLUMNS, data)]
    return Ledger(
        {_key(t.position.arm, t.position.strategy, t.position.market_id): t for t in trades},
        version=version,
    )


def _has_legacy_records(storage: Storage) -> bool:
    """Baseline trades every rules-passing candidate, so a store with trades has baseline ones."""
    return any(storage.list(positions_prefix("baseline", s)) for s in STRATEGIES)


def load_ledger(storage: Storage) -> Ledger:
    """One read. A missing table is an empty ledger only on a store that never had trades."""
    stored = storage.read_versioned(TRADES_KEY)
    if stored.data is None:
        if _has_legacy_records(storage):
            raise MissingTradesTable(TRADES_KEY)
        return Ledger()
    return decode_ledger(stored.data, stored.version)


def save_ledger(storage: Storage, ledger: Ledger) -> None:
    """One conditional write, skipped when nothing changed."""
    if not ledger.changed:
        return
    ledger.version = storage.write_versioned(TRADES_KEY, encode_ledger(ledger), ledger.version)
    ledger.changed = False
