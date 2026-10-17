import json
from pathlib import Path

import pytest

from bondhype.config import load_config
from bondhype.entry import open_position
from bondhype.ledger import Ledger
from bondhype.models import Book, FeeSchedule
from builders import NOW, make_book, make_market

CONFIG = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")
DEEP_BOOK = make_book(bid=0.92, ask=0.93, ask_size=1000.0)


def enter(ledger, market=None, book=DEEP_BOOK, arm="baseline", strategy="bond", now=NOW):
    return open_position(
        CONFIG,
        ledger,
        arm=arm,
        strategy=strategy,
        market=market or make_market(),
        side="NO",
        book=book,
        now=now,
    )


def test_open_position_logs_every_cost_field_and_updates_the_portfolio():
    ledger = Ledger()

    result = enter(ledger)

    assert result.rejection is None
    position = result.position
    assert position.shares == pytest.approx(10 / 0.93)
    assert position.fee_usd == pytest.approx(0.035, abs=1e-5)
    assert position.gross_edge_per_share == pytest.approx(0.07)
    assert position.slippage_per_share == pytest.approx(0.0)
    assert position.config_version == "2026-10-04.1"

    assert ledger.positions("baseline", "bond") == [position]
    assert ledger.changed

    portfolio = ledger.portfolio(CONFIG, "baseline", "bond")
    assert portfolio.exposure_usd == pytest.approx(10.0)
    assert portfolio.balance_usd == pytest.approx(1000 - 10.035, abs=1e-5)


def test_second_entry_for_the_same_arm_strategy_and_market_is_a_duplicate():
    ledger = Ledger()
    first = enter(ledger).position

    later_book = make_book(bid=0.93, ask=0.94, ask_size=1000.0)
    result = enter(ledger, book=later_book)

    assert result.position is None
    assert result.rejection == "duplicate_position"
    assert ledger.positions("baseline", "bond") == [first]


def test_same_market_may_be_entered_in_another_arm_and_another_strategy():
    ledger = Ledger()
    assert enter(ledger, arm="baseline").position is not None
    assert enter(ledger, arm="prompt_buy").position is not None
    assert enter(ledger, arm="baseline", strategy="hype").position is not None


def test_sixth_position_in_one_event_is_rejected_but_another_event_is_not():
    ledger = Ledger()
    for i in range(5):
        assert enter(ledger, make_market(id=f"m{i}", event_id="e1")).position is not None

    capped = enter(ledger, make_market(id="m5", event_id="e1"))
    other_event = enter(ledger, make_market(id="m6", event_id="e2"))

    assert capped.position is None
    assert capped.rejection == "event_cap"
    assert other_event.position is not None


@pytest.mark.parametrize(
    "market_overrides,book,rejection",
    [
        pytest.param({"fee_schedule": None}, DEEP_BOOK, "fee_schedule_missing", id="no-fees"),
        pytest.param(
            {"fee_schedule": FeeSchedule(rate=0.05, exponent=2, enabled=True)},
            DEEP_BOOK,
            "unsupported_fee_schedule",
            id="exponent",
        ),
        pytest.param({}, Book(bids=(), asks=()), "no_liquidity", id="empty-book"),
    ],
)
def test_entry_fails_closed_and_records_nothing(market_overrides, book, rejection):
    ledger = Ledger()

    result = enter(ledger, make_market(**market_overrides), book=book)

    assert result.position is None
    assert result.rejection == rejection
    assert ledger.trades == {} and not ledger.changed


@pytest.mark.parametrize(
    "fixture,best_ask,expected_fee",
    [
        ("bond_sports_tight", 0.93, 0.021),  # sports market, own rate 0.03
        ("overlap_bond_priority_hype_yes_below_25", 0.91, 0.045),  # culture, rate 0.05
    ],
)
def test_recorded_market_and_book_open_a_position_with_the_markets_own_fee(
    fixture, best_ask, expected_fee
):
    from bondhype.books import parse_book
    from bondhype.markets import parse_market

    recorded = json.loads(
        (Path(__file__).parent / "fixtures" / "live" / f"{fixture}.json").read_text()
    )
    market = parse_market(recorded["gamma_market"])
    book = parse_book(recorded["clob_book"])
    result = enter(Ledger(), market, book=book)

    position = result.position
    assert position.best_ask == best_ask
    assert position.avg_price == pytest.approx(best_ask)
    assert position.slippage_per_share == pytest.approx(0.0)
    assert position.gross_edge_per_share == pytest.approx(1 - best_ask)
    assert position.fee_usd == pytest.approx(expected_fee, abs=1e-5)
