import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bondhype.portfolio import PositionOpened
from bondhype.settlement import assess_overdue, resolve

LIVE = Path(__file__).parent / "fixtures" / "live"
OPENED_AT = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((LIVE / f"{name}.json").read_text())


def position(market_id: str, side: str = "NO", **overrides) -> PositionOpened:
    fields = dict(
        arm="baseline",
        strategy="bond",
        market_id=market_id,
        event_id="e1",
        side=side,
        opened_at=OPENED_AT,
        config_version="v1",
        shares=10.0,
        filled_usd=9.30,
        avg_price=0.93,
        best_ask=0.93,
        slippage_per_share=0.0,
        gross_edge_per_share=0.07,
        fee_usd=0.05,
        partial=False,
    )
    fields.update(overrides)
    return PositionOpened(**fields)


def test_winning_no_position_pays_one_dollar_per_share():
    raw = load("settle_resolved_no")

    resolution = resolve(position(raw["id"], side="NO"), raw)

    assert resolution.outcome == "win"
    assert resolution.payout_usd == pytest.approx(10.0)
    assert resolution.pnl_usd == pytest.approx(10.0 - 9.30 - 0.05)


def test_losing_no_position_pays_nothing_and_loses_stake_and_fee():
    raw = load("settle_resolved_yes")

    resolution = resolve(position(raw["id"], side="NO"), raw)

    assert resolution.outcome == "loss"
    assert resolution.payout_usd == 0.0
    assert resolution.pnl_usd == pytest.approx(-9.30 - 0.05)


@pytest.mark.parametrize("name", ["settle_overdue_open", "settle_disputed"])
def test_market_that_is_not_resolved_does_not_settle(name):
    raw = load(name)

    assert resolve(position(raw["id"]), raw) is None


@pytest.mark.parametrize("prices", ['["0.5", "0.5"]', '["0.9995", "0.0005"]', "[]", "not json"])
def test_closed_market_without_a_clean_binary_result_does_not_settle(prices):
    raw = load("settle_resolved_no") | {"outcomePrices": prices}

    assert resolve(position(raw["id"]), raw) is None


def test_resolution_records_identity_timing_and_cost_basis():
    raw = load("settle_resolved_yes")  # end 2024-11-05T12:00Z, closed 2024-11-06 15:17:41+00

    resolution = resolve(position(raw["id"], arm="mix_and", strategy="hype"), raw)

    assert (resolution.arm, resolution.strategy, resolution.market_id, resolution.side) == (
        "mix_and",
        "hype",
        raw["id"],
        "NO",
    )
    assert resolution.config_version == "v1"
    assert resolution.resolved_at == datetime(2024, 11, 6, 15, 17, 41, tzinfo=UTC)
    assert resolution.days_overdue == pytest.approx(98261 / 86400)


def test_yes_position_wins_when_market_resolves_yes():
    raw = load("settle_resolved_yes")

    resolution = resolve(position(raw["id"], side="YES"), raw)

    assert resolution.outcome == "win"
    assert resolution.payout_usd == pytest.approx(10.0)


def test_dispute_history_is_kept_on_the_resolution():
    raw = load("settle_resolved_no")
    undisputed = resolve(position(raw["id"]), raw)
    statuses = ["proposed", "disputed", "proposed", "resolved"]
    disputed_raw = raw | {"umaResolutionStatuses": json.dumps(statuses)}

    disputed = resolve(position(raw["id"]), disputed_raw)

    assert undisputed.was_disputed is False
    assert disputed.was_disputed is True
    assert disputed.uma_statuses == ("proposed", "disputed", "proposed", "resolved")


@pytest.mark.parametrize("closed_time", [None, "", "garbage"])
def test_resolved_market_without_a_readable_close_time_does_not_settle(closed_time):
    raw = load("settle_resolved_no") | {"closedTime": closed_time}

    assert resolve(position(raw["id"]), raw) is None


def test_position_is_not_overdue_before_the_end_date():
    raw = load("settle_overdue_open")  # ends 2026-10-01T03:59Z
    now = datetime(2026, 10, 1, 3, 58, tzinfo=UTC)

    assert assess_overdue(position(raw["id"]), raw, now) is None


def test_overdue_position_reports_age_status_price_and_conservative_loss():
    raw = load("settle_overdue_open")  # NO price 0.9995, no UMA status yet
    now = datetime(2026, 10, 3, 3, 59, tzinfo=UTC)

    report = assess_overdue(position(raw["id"], side="NO"), raw, now)

    assert report.days_overdue == pytest.approx(2.0)
    assert report.uma_status is None
    assert report.closed is False
    assert report.held_side_price == pytest.approx(0.9995)
    assert report.was_disputed is False
    assert report.conservative_pnl_usd == pytest.approx(-9.30 - 0.05)


def test_overdue_report_flags_a_dispute_in_progress():
    raw = load("settle_disputed")
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    report = assess_overdue(position(raw["id"]), raw, now)

    assert report.uma_status == "disputed"
    assert report.was_disputed is True


def test_resolved_market_is_settled_not_overdue():
    raw = load("settle_resolved_no")
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    assert assess_overdue(position(raw["id"]), raw, now) is None
