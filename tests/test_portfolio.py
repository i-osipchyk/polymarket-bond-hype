from datetime import UTC, datetime

import pytest

from bondhype.portfolio import Portfolio, PositionOpened

OPENED_AT = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def opened(market_id, event_id, filled_usd, fee_usd, **overrides) -> PositionOpened:
    fields = dict(
        arm="baseline",
        strategy="bond",
        market_id=market_id,
        event_id=event_id,
        side="NO",
        opened_at=OPENED_AT,
        config_version="v1",
        shares=filled_usd / 0.93,
        filled_usd=filled_usd,
        avg_price=0.93,
        best_ask=0.93,
        slippage_per_share=0.0,
        gross_edge_per_share=0.07,
        fee_usd=fee_usd,
        partial=False,
    )
    fields.update(overrides)
    return PositionOpened(**fields)


def test_portfolio_is_derived_from_position_events():
    events = [
        opened("m1", "e1", 10.00, 0.04),
        opened("m2", "e1", 10.00, 0.05),
        opened("m3", "e2", 6.53, 0.02, partial=True),
    ]

    portfolio = Portfolio.from_events(events, starting_balance_usd=1000.0)

    assert portfolio.balance_usd == pytest.approx(973.36)
    assert portfolio.exposure_usd == pytest.approx(26.53)
    assert portfolio.bankroll_usd == pytest.approx(999.89)
    assert portfolio.open_positions_by_event == {"e1": 2, "e2": 1}
    assert portfolio.open_market_ids == {"m1", "m2", "m3"}
