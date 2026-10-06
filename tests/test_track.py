import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bondhype.books import BookFetchError
from bondhype.config import load_config
from bondhype.portfolio import load_portfolio, position_key
from bondhype.settlement import Resolution
from bondhype.storage import LocalStorage
from bondhype.track import track
from test_settlement import load, position

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def store(storage, pos):
    storage.put(position_key(pos.arm, pos.strategy, pos.market_id), pos.to_json())


def markets(*raws):
    by_id = {raw["id"]: raw for raw in raws}
    return lambda market_id: by_id[market_id]


def no_book(token_id):
    raise AssertionError("no book expected")


def test_resolved_position_is_settled_once_and_stored(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_resolved_no")
    store(storage, position(raw["id"], arm="mix_or", strategy="hype"))

    for _ in range(2):  # a retried run must not duplicate or change the record
        track(storage, markets(raw), no_book, NOW)

    (key,) = storage.list("arms/mix_or/hype/resolutions/")
    assert key == f"arms/mix_or/hype/resolutions/{raw['id']}.json"
    stored = Resolution.from_json(storage.get(key))
    assert stored.outcome == "win"
    assert stored.pnl_usd == pytest.approx(10.0 - 9.30 - 0.05)


def test_open_position_gets_a_price_point_and_book_snapshot_of_the_held_side(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_overdue_open")  # NO price 0.9995
    store(storage, position(raw["id"], arm="baseline", strategy="bond", side="NO"))
    no_token = json.loads(raw["clobTokenIds"])[1]
    book = {"bids": [{"price": "0.99", "size": "50"}], "asks": [{"price": "1", "size": "5"}]}
    fetched = []

    def fetch_book(token_id):
        fetched.append(token_id)
        return book

    track(storage, markets(raw), fetch_book, NOW)

    assert fetched == [no_token]
    (key,) = storage.list("arms/baseline/bond/pricepath/")
    assert key == f"arms/baseline/bond/pricepath/{raw['id']}/2026-11-10T12.json"
    point = json.loads(storage.get(key))
    assert point["observed_at"] == NOW.isoformat()
    assert point["yes_price"] == pytest.approx(0.0005)
    assert point["no_price"] == pytest.approx(0.9995)
    assert point["book"] == book
    assert storage.list("arms/baseline/bond/resolutions/") == []


def test_settled_position_is_not_fetched_again(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_resolved_no")
    store(storage, position(raw["id"]))
    track(storage, markets(raw), no_book, NOW)

    def fail(_):
        raise AssertionError("settled position must not be fetched")

    track(storage, fail, no_book, NOW)


def test_a_failed_market_fetch_does_not_block_other_positions(tmp_path):
    storage = LocalStorage(tmp_path)
    broken = load("settle_overdue_open")
    resolved = load("settle_resolved_no")
    store(storage, position(broken["id"]))
    store(storage, position(resolved["id"]))

    def fetch_market(market_id):
        if market_id == broken["id"]:
            raise OSError("gamma down")
        return resolved

    summary = track(storage, fetch_market, no_book, NOW)

    assert storage.list("arms/baseline/bond/resolutions/") == [
        f"arms/baseline/bond/resolutions/{resolved['id']}.json"
    ]
    assert summary.errors == [(broken["id"], "OSError('gamma down')")]


def test_a_failed_book_fetch_still_records_the_price_point(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_overdue_open")
    store(storage, position(raw["id"]))

    def fetch_book(token_id):
        raise BookFetchError("clob down")

    track(storage, markets(raw), fetch_book, NOW)

    (key,) = storage.list("arms/baseline/bond/pricepath/")
    point = json.loads(storage.get(key))
    assert point["book"] is None
    assert point["no_price"] == pytest.approx(0.9995)


def test_settling_frees_exposure_in_the_portfolio_that_entry_reads(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_resolved_no")
    store(storage, position(raw["id"]))
    config = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")

    before = load_portfolio(storage, config, "baseline", "bond")
    track(storage, markets(raw), no_book, NOW)
    after = load_portfolio(storage, config, "baseline", "bond")

    assert before.exposure_usd == pytest.approx(9.30)
    assert after.exposure_usd == 0.0
    assert after.balance_usd == pytest.approx(1000.0 - 9.30 - 0.05 + 10.0)
