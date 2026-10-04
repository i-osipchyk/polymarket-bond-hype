import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bondhype.books import ParseError, parse_book
from bondhype.markets import parse_market

LIVE = Path(__file__).parent / "fixtures" / "live"


def load(name: str) -> dict:
    return json.loads((LIVE / f"{name}.json").read_text())


def test_parse_book_reads_every_level_as_numbers():
    book = parse_book(load("bond_sports_tight")["clob_book"])

    assert len(book.asks) == 7
    assert len(book.bids) == 32
    best_ask = min(book.asks, key=lambda level: level.price)
    best_bid = max(book.bids, key=lambda level: level.price)
    assert (best_ask.price, best_ask.size) == (0.93, 2967.31)
    assert (best_bid.price, best_bid.size) == (0.92, 305.0)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda raw: raw.pop("bids"), id="missing-bids"),
        pytest.param(lambda raw: raw.pop("asks"), id="missing-asks"),
        pytest.param(lambda raw: raw["asks"][0].update(price="n/a"), id="non-numeric-price"),
        pytest.param(lambda raw: raw["bids"][0].pop("size"), id="missing-size"),
    ],
)
def test_parse_book_rejects_schema_drift(mutate):
    raw = load("bond_sports_tight")["clob_book"]
    mutate(raw)
    with pytest.raises(ParseError):
        parse_book(raw)


def test_parse_market_reads_gamma_row_from_recorded_fixture():
    market = parse_market(load("bond_sports_tight")["gamma_market"])

    assert market.id == "5102083"
    assert market.question == "Hawai'i vs. Arizona State"
    assert market.end_date == datetime(2026, 10, 11, 2, 30, tzinfo=UTC)
    assert market.volume_usd == pytest.approx(4330.287648)
    assert (market.yes_price, market.no_price) == (0.075, 0.925)
    assert market.event_id == "1100631"
    assert market.yes_token.startswith("44468399620238")
    assert market.no_token.startswith("66598661988093")
    assert market.outcomes == ("Hawai'i", "Arizona State")


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda raw: raw.pop("endDate"), id="missing-end-date"),
        pytest.param(lambda raw: raw.pop("volumeNum"), id="missing-volume"),
        pytest.param(lambda raw: raw.update(events=[]), id="no-event"),
        pytest.param(lambda raw: raw.update(outcomePrices="not json"), id="bad-prices-json"),
        pytest.param(lambda raw: raw.update(outcomePrices='["0.3","0.3","0.4"]'), id="three-way"),
        pytest.param(lambda raw: raw.update(endDate="soon"), id="bad-date"),
    ],
)
def test_parse_market_rejects_schema_drift(mutate):
    raw = load("bond_sports_tight")["gamma_market"]
    mutate(raw)
    with pytest.raises(ParseError):
        parse_market(raw)


@pytest.mark.parametrize(
    "fixture,rate",
    [
        ("bond_sports_tight", 0.03),
        ("hype_no_side", 0.07),
        ("bond_slow_14d", 0.04),
        ("overlap_bond_priority_hype_yes_below_25", 0.05),
    ],
)
def test_parse_market_reads_the_markets_own_fee_schedule(fixture, rate):
    fees = parse_market(load(fixture)["gamma_market"]).fee_schedule
    assert (fees.rate, fees.exponent, fees.enabled) == (rate, 1, True)


def test_market_without_a_fee_schedule_has_none():
    raw = load("bond_sports_tight")["gamma_market"]
    raw.pop("feeSchedule")
    assert parse_market(raw).fee_schedule is None


def test_market_with_fees_disabled_and_no_schedule_is_fee_free():
    raw = load("bond_sports_tight")["gamma_market"]
    raw.pop("feeSchedule")
    raw["feesEnabled"] = False
    fees = parse_market(raw).fee_schedule
    assert (fees.rate, fees.enabled) == (0.0, False)
