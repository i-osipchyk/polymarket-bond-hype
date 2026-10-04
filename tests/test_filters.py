from datetime import timedelta
from pathlib import Path

import pytest

from bondhype.config import load_config
from bondhype.filters import evaluate_bond, evaluate_hype, evaluate_market
from bondhype.models import Book, Level
from builders import NOW, make_book, make_market

CONFIG = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")


def test_healthy_bond_candidate_passes_and_reports_raw_values():
    result = evaluate_bond(make_market(), "NO", make_book(), CONFIG, NOW)

    assert result.passed
    assert result.reasons == ()
    assert result.values["ask"] == 0.93
    assert result.values["spread"] == pytest.approx(0.01)
    assert result.values["days_to_resolution"] == pytest.approx(6.0)
    assert result.values["liquidity_usd"] == pytest.approx(0.93 * 100 + 0.94 * 100)
    assert result.values["annualised_yield"] == pytest.approx(4.58, abs=0.01)


@pytest.mark.parametrize("ask", [0.90, 0.95])
def test_bond_price_band_boundaries_are_inclusive(ask):
    book = make_book(bid=round(ask - 0.01, 2), ask=ask)
    assert evaluate_bond(make_market(), "NO", book, CONFIG, NOW).passed


@pytest.mark.parametrize("ask", [0.89, 0.96])
def test_bond_rejects_ask_outside_price_band(ask):
    book = make_book(bid=round(ask - 0.01, 2), ask=ask)
    result = evaluate_bond(make_market(), "NO", book, CONFIG, NOW)
    assert not result.passed
    assert result.reasons == ("price_out_of_band",)


@pytest.mark.parametrize("days", [1, 14])
def test_bond_days_to_resolution_boundaries_are_inclusive(days):
    market = make_market(end_date=NOW + timedelta(days=days))
    book = make_book(bid=0.91, ask=0.92)  # yield stays above threshold even at 14 days
    assert evaluate_bond(market, "NO", book, CONFIG, NOW).passed


@pytest.mark.parametrize("days", [-1, 0, 0.5, 14.5])
def test_bond_rejects_resolution_outside_window(days):
    market = make_market(end_date=NOW + timedelta(days=days))
    result = evaluate_bond(market, "NO", make_book(), CONFIG, NOW)
    assert not result.passed
    assert "days_out_of_range" in result.reasons


def test_bond_spread_of_exactly_two_cents_passes():
    book = make_book(bid=0.91, ask=0.93)
    assert evaluate_bond(make_market(), "NO", book, CONFIG, NOW).passed


def test_bond_rejects_spread_wider_than_two_cents():
    book = make_book(bid=0.90, ask=0.93)
    result = evaluate_bond(make_market(), "NO", book, CONFIG, NOW)
    assert not result.passed
    assert result.reasons == ("spread_too_wide",)


def test_bond_rejects_book_holding_less_than_three_times_order_size():
    # 10 shares at 0.93 + 10 at 0.94 = $18.70 on the two best ask levels, below $30
    book = make_book(ask_size=10.0, next_ask_size=10.0)
    result = evaluate_bond(make_market(), "NO", book, CONFIG, NOW)
    assert not result.passed
    assert result.reasons == ("insufficient_liquidity",)
    assert result.values["liquidity_usd"] == pytest.approx(18.7)


def test_bond_liquidity_is_judged_on_best_levels_not_input_order():
    deep_but_far = Level(0.99, 10_000.0)
    book = Book(
        bids=(Level(0.92, 500.0),),
        asks=(deep_but_far, Level(0.94, 10.0), Level(0.93, 10.0)),
    )
    result = evaluate_bond(make_market(), "NO", book, CONFIG, NOW)
    assert result.reasons == ("insufficient_liquidity",)


def test_bond_volume_floor_is_inclusive():
    result = evaluate_bond(make_market(volume_usd=2000.0), "NO", make_book(), CONFIG, NOW)
    assert result.passed


def test_bond_rejects_volume_below_floor():
    result = evaluate_bond(make_market(volume_usd=1999.0), "NO", make_book(), CONFIG, NOW)
    assert not result.passed
    assert result.reasons == ("volume_below_floor",)
    assert result.values["volume_usd"] == 1999.0


@pytest.mark.parametrize("ask", [0.95, 0.93])
def test_bond_rejects_low_annualised_yield_at_fourteen_days(ask):
    # 14 days: 95c -> 137%, 93c -> 196%, both below the 200% threshold
    market = make_market(end_date=NOW + timedelta(days=14))
    book = make_book(bid=round(ask - 0.01, 2), ask=ask)
    result = evaluate_bond(market, "NO", book, CONFIG, NOW)
    assert not result.passed
    assert result.reasons == ("yield_below_threshold",)


def test_bond_accepts_adequate_annualised_yield_at_fourteen_days():
    # 92c at 14 days -> 227%
    market = make_market(end_date=NOW + timedelta(days=14))
    book = make_book(bid=0.91, ask=0.92)
    assert evaluate_bond(market, "NO", book, CONFIG, NOW).passed


@pytest.mark.parametrize(
    "bids,asks",
    [
        pytest.param((), (Level(0.93, 100.0),), id="no-bids"),
        pytest.param((Level(0.92, 100.0),), (), id="no-asks"),
        pytest.param((), (), id="empty-book"),
    ],
)
def test_bond_fails_closed_on_missing_book_side(bids, asks):
    result = evaluate_bond(make_market(), "NO", Book(bids=bids, asks=asks), CONFIG, NOW)
    assert not result.passed
    assert result.reasons == ("missing_book_data",)


def test_healthy_hype_candidate_passes_without_a_yield_check():
    # 14 days at a 95c NO ask would fail bond's yield rule; hype has no such rule
    market = make_market(yes_price=0.10, no_price=0.90, end_date=NOW + timedelta(days=14))
    book = make_book(bid=0.94, ask=0.95)
    result = evaluate_hype(market, book, CONFIG, NOW)
    assert result.passed
    assert result.reasons == ()
    assert result.values["yes_price"] == 0.10
    assert result.values["ask"] == 0.95


def _hype(yes_price, **market_overrides):
    market = make_market(yes_price=yes_price, no_price=round(1 - yes_price, 4), **market_overrides)
    return market


@pytest.mark.parametrize("yes_price", [0.05, 0.25])
def test_hype_yes_band_boundaries_are_inclusive(yes_price):
    book = make_book(bid=0.80, ask=0.81)
    assert evaluate_hype(_hype(yes_price), book, CONFIG, NOW).passed


@pytest.mark.parametrize("yes_price", [0.04, 0.26])
def test_hype_rejects_yes_price_outside_band(yes_price):
    book = make_book(bid=0.80, ask=0.81)
    result = evaluate_hype(_hype(yes_price), book, CONFIG, NOW)
    assert not result.passed
    assert result.reasons == ("yes_price_out_of_band",)


def test_hype_allows_three_cent_spread_that_bond_would_reject():
    book = make_book(bid=0.78, ask=0.81)
    assert evaluate_hype(_hype(0.19), book, CONFIG, NOW).passed


def test_hype_rejects_spread_wider_than_three_cents():
    book = make_book(bid=0.77, ask=0.81)
    result = evaluate_hype(_hype(0.19), book, CONFIG, NOW)
    assert result.reasons == ("spread_too_wide",)


@pytest.mark.parametrize(
    "market_overrides,book,reason",
    [
        pytest.param(
            {"end_date": NOW + timedelta(days=15)}, make_book(0.80, 0.81), "days_out_of_range"
        ),
        pytest.param(
            {"end_date": NOW + timedelta(hours=12)}, make_book(0.80, 0.81), "days_out_of_range"
        ),
        pytest.param({"volume_usd": 1999.0}, make_book(0.80, 0.81), "volume_below_floor"),
        pytest.param(
            {}, make_book(0.80, 0.81, ask_size=10.0, next_ask_size=10.0), "insufficient_liquidity"
        ),
    ],
)
def test_hype_applies_the_shared_rules(market_overrides, book, reason):
    result = evaluate_hype(_hype(0.19, **market_overrides), book, CONFIG, NOW)
    assert result.reasons == (reason,)


def test_hype_fails_closed_on_missing_book_side():
    result = evaluate_hype(_hype(0.19), Book(bids=(), asks=()), CONFIG, NOW)
    assert not result.passed
    assert result.reasons == ("missing_book_data",)


def test_bond_takes_priority_when_both_strategies_would_pass():
    # NO is 91c (bond band) and YES is 9.5c (hype band): one label only, bond
    market = _hype(0.095)
    books = {"YES": make_book(0.09, 0.10), "NO": make_book(0.90, 0.91)}
    evaluation = evaluate_market(market, books, CONFIG, NOW)
    assert evaluation.selected.strategy == "bond"
    assert evaluation.selected.side == "NO"


def test_hype_only_market_is_labelled_hype():
    books = {"YES": make_book(0.18, 0.19), "NO": make_book(0.80, 0.81)}
    evaluation = evaluate_market(_hype(0.19), books, CONFIG, NOW)
    assert (evaluation.selected.strategy, evaluation.selected.side) == ("hype", "NO")


def test_rejected_market_is_still_evaluated_with_reasons_for_every_attempt():
    market = _hype(0.50, volume_usd=100.0)
    books = {"YES": make_book(0.49, 0.50), "NO": make_book(0.49, 0.50)}
    evaluation = evaluate_market(market, books, CONFIG, NOW)
    assert evaluation.selected is None
    assert [(a.strategy, a.side) for a in evaluation.attempts] == [
        ("bond", "YES"),
        ("bond", "NO"),
        ("hype", "NO"),
    ]
    assert all(not a.result.passed and a.result.reasons for a in evaluation.attempts)


def test_market_with_a_missing_side_book_fails_closed():
    evaluation = evaluate_market(_hype(0.19), {"NO": make_book(0.80, 0.81)}, CONFIG, NOW)
    bond_yes = evaluation.attempts[0]
    assert bond_yes.result.reasons == ("missing_book_data",)
    assert evaluation.selected.strategy == "hype"  # the NO side is still judged on its own book
