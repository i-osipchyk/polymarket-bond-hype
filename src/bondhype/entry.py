from dataclasses import dataclass
from datetime import datetime

from bondhype.config import Config
from bondhype.fills import UnsupportedFeeSchedule, simulate_fill
from bondhype.ledger import Ledger
from bondhype.models import Book, Market
from bondhype.portfolio import PositionOpened


@dataclass(frozen=True)
class EntryResult:
    position: PositionOpened | None
    rejection: str | None


def open_position(
    config: Config,
    ledger: Ledger,
    *,
    arm: str,
    strategy: str,
    market: Market,
    side: str,
    book: Book,
    now: datetime,
) -> EntryResult:
    """Checks run against the in-memory ledger, which the new position is added to; the caller
    saves the ledger once at the end of the run."""
    if ledger.holds(arm, strategy, market.id):
        return EntryResult(position=None, rejection="duplicate_position")
    portfolio = ledger.portfolio(config, arm, strategy)
    open_in_event = portfolio.open_positions_by_event.get(market.event_id, 0)
    if open_in_event >= config.portfolio.max_positions_per_event:
        return EntryResult(position=None, rejection="event_cap")
    if market.fee_schedule is None:
        return EntryResult(position=None, rejection="fee_schedule_missing")
    try:
        fill = simulate_fill(book, config.order_size_usd, market.fee_schedule)
    except UnsupportedFeeSchedule:
        return EntryResult(position=None, rejection="unsupported_fee_schedule")
    if fill is None:
        return EntryResult(position=None, rejection="no_liquidity")
    position = PositionOpened(
        arm=arm,
        strategy=strategy,
        market_id=market.id,
        event_id=market.event_id,
        side=side,
        opened_at=now,
        config_version=config.version,
        shares=fill.shares,
        filled_usd=fill.filled_usd,
        avg_price=fill.avg_price,
        best_ask=fill.best_ask,
        slippage_per_share=fill.slippage_per_share,
        gross_edge_per_share=fill.gross_edge_per_share,
        fee_usd=fill.fee_usd,
        partial=fill.partial,
    )
    ledger.add_position(position)
    return EntryResult(position=position, rejection=None)
