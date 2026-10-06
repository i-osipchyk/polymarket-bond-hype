from datetime import UTC, datetime, timedelta

from bondhype.models import Book, FeeSchedule, Level, Market

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def make_market(**overrides) -> Market:
    fields = dict(
        id="m1",
        question="Will X happen?",
        end_date=NOW + timedelta(days=6),
        volume_usd=10_000.0,
        yes_price=0.07,
        no_price=0.93,
        event_id="e1",
        fee_schedule=FeeSchedule(rate=0.05, exponent=1, enabled=True),
    )
    fields.update(overrides)
    return Market(**fields)


def make_book(bid=0.92, ask=0.93, ask_size=100.0, next_ask_size=100.0) -> Book:
    return Book(
        bids=(Level(bid, 500.0),),
        asks=(Level(ask, ask_size), Level(round(ask + 0.01, 4), next_ask_size)),
    )


def make_position(market_id="m1", *, shares=10.0, filled_usd=9.4, fee_usd=0.05, **overrides):
    from bondhype.portfolio import PositionOpened

    fields = dict(
        arm="baseline",
        strategy="bond",
        market_id=market_id,
        event_id=f"e-{market_id}",
        side="NO",
        opened_at=NOW,
        config_version="v1",
        shares=shares,
        filled_usd=filled_usd,
        avg_price=filled_usd / shares,
        best_ask=filled_usd / shares,
        slippage_per_share=0.0,
        gross_edge_per_share=0.06,
        fee_usd=fee_usd,
        partial=False,
    )
    fields.update(overrides)
    return PositionOpened(**fields)


def make_resolution(position, *, won=True, days_to_resolve=5.0, **overrides):
    from bondhype.settlement import Resolution

    payout = position.shares if won else 0.0
    fields = dict(
        arm=position.arm,
        strategy=position.strategy,
        market_id=position.market_id,
        side=position.side,
        config_version=position.config_version,
        outcome="win" if won else "loss",
        payout_usd=payout,
        pnl_usd=payout - position.filled_usd - position.fee_usd,
        resolved_at=position.opened_at + timedelta(days=days_to_resolve),
        days_overdue=0.0,
        uma_statuses=("resolved",),
    )
    fields.update(overrides)
    return Resolution(**fields)


def make_overdue(position, **overrides):
    from bondhype.settlement import OverdueReport

    fields = dict(
        arm=position.arm,
        strategy=position.strategy,
        market_id=position.market_id,
        config_version=position.config_version,
        days_overdue=3.0,
        closed=False,
        uma_status="proposed",
        uma_statuses=("proposed",),
        held_side_price=0.97,
        conservative_pnl_usd=-(position.filled_usd + position.fee_usd),
    )
    fields.update(overrides)
    return OverdueReport(**fields)
