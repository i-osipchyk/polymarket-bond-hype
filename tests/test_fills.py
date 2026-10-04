import pytest

from bondhype.fills import UnsupportedFeeSchedule, simulate_fill
from bondhype.models import Book, FeeSchedule, Level
from builders import make_book

FEE_5PCT = FeeSchedule(rate=0.05, exponent=1, enabled=True)


def test_single_level_fill_reports_price_shares_fee_and_edge_separately():
    book = make_book(bid=0.92, ask=0.93, ask_size=1000.0)

    fill = simulate_fill(book, order_usd=10.0, fees=FEE_5PCT)

    assert fill.best_ask == 0.93
    assert fill.avg_price == pytest.approx(0.93)
    assert fill.shares == pytest.approx(10 / 0.93)
    assert fill.filled_usd == pytest.approx(10.0)
    assert not fill.partial
    assert fill.slippage_per_share == pytest.approx(0.0)
    assert fill.gross_edge_per_share == pytest.approx(0.07)
    # $10 of shares at 93c: fee = 10 * 0.05 * (1 - 0.93) = 0.035
    assert fill.fee_usd == pytest.approx(0.035, abs=1e-5)


def test_fill_walks_into_the_second_level_and_reports_slippage():
    # 5 shares at 0.93 ($4.65), then $5.35 more at 0.94
    book = make_book(bid=0.92, ask=0.93, ask_size=5.0, next_ask_size=100.0)

    fill = simulate_fill(book, order_usd=10.0, fees=FEE_5PCT)

    assert fill.shares == pytest.approx(10.691489, abs=1e-5)
    assert fill.avg_price == pytest.approx(0.935323, abs=1e-5)
    assert fill.best_ask == 0.93
    assert fill.slippage_per_share == pytest.approx(0.005323, abs=1e-5)
    assert fill.gross_edge_per_share == pytest.approx(0.07)
    assert fill.fee_usd == pytest.approx(0.032325, abs=1e-5)
    assert not fill.partial


def test_thin_book_gives_a_partial_fill_of_whatever_is_available():
    # depth: 5 @ 0.93 + 2 @ 0.94 = $6.53 against a $10 order
    book = make_book(bid=0.92, ask=0.93, ask_size=5.0, next_ask_size=2.0)

    fill = simulate_fill(book, order_usd=10.0, fees=FEE_5PCT)

    assert fill.partial
    assert fill.shares == pytest.approx(7.0)
    assert fill.filled_usd == pytest.approx(6.53)
    assert fill.avg_price == pytest.approx(6.53 / 7.0)


@pytest.mark.parametrize(
    "asks",
    [pytest.param((), id="no-asks"), pytest.param((Level(0.93, 0.0),), id="zero-size-level")],
)
def test_nothing_to_fill_returns_none(asks):
    book = Book(bids=(Level(0.92, 100.0),), asks=asks)
    assert simulate_fill(book, order_usd=10.0, fees=FEE_5PCT) is None


def test_fee_free_market_charges_no_fee():
    book = make_book(bid=0.92, ask=0.93, ask_size=1000.0)
    fill = simulate_fill(book, 10.0, FeeSchedule(rate=0.05, exponent=1, enabled=False))
    assert fill.fee_usd == 0.0


def test_unverified_fee_exponent_fails_closed():
    book = make_book(bid=0.92, ask=0.93, ask_size=1000.0)
    with pytest.raises(UnsupportedFeeSchedule):
        simulate_fill(book, 10.0, FeeSchedule(rate=0.05, exponent=2, enabled=True))
