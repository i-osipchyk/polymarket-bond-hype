import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bondhype.books import BookFetchError
from bondhype.config import load_config
from bondhype.ledger import load_ledger, save_ledger
from bondhype.storage import LocalStorage
from bondhype.track import track
from test_settlement import load, position

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def store(storage, pos):
    ledger = load_ledger(storage)
    ledger.add_position(pos)
    save_ledger(storage, ledger)


def resolutions(storage, arm="baseline", strategy="bond"):
    return load_ledger(storage).resolutions(arm, strategy)


def markets(*raws):
    by_id = {raw["id"]: raw for raw in raws}
    return lambda market_id: by_id[market_id]


def no_book(token_id):
    raise AssertionError("no book expected")


def test_resolved_position_is_settled_once_in_the_trades_table(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_resolved_no")
    store(storage, position(raw["id"], arm="mix_or", strategy="hype"))

    for _ in range(2):  # a retried run must not duplicate or change the record
        track(storage, markets(raw), no_book, NOW)

    (stored,) = resolutions(storage, "mix_or", "hype")
    assert stored.market_id == raw["id"]
    assert load_ledger(storage).trades[("mix_or", "hype", raw["id"])].status == "resolved"
    assert stored.outcome == "win"
    assert stored.pnl_usd == pytest.approx(10.0 - 9.30 - 0.05)


def price_points(storage):
    (key,) = storage.list("pricepath/")
    return key, [json.loads(line) for line in storage.get(key).decode().splitlines()]


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
    key, (point,) = price_points(storage)
    assert key == "pricepath/date=2026-11-10/run=2026-11-10T12.jsonl"
    assert point["market_id"] == raw["id"]
    assert point["holders"] == [{"arm": "baseline", "strategy": "bond", "side": "NO"}]
    assert point["observed_at"] == NOW.isoformat()
    assert point["yes_price"] == pytest.approx(0.0005)
    assert point["no_price"] == pytest.approx(0.9995)
    assert point["books"] == {"NO": book}
    assert resolutions(storage) == []


def test_a_market_held_by_several_arms_is_fetched_and_recorded_once(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_overdue_open")
    for arm in ("baseline", "prompt_buy", "mix_or"):
        store(storage, position(raw["id"], arm=arm, strategy="bond", side="NO"))
    fetched_markets, fetched_books = [], []

    def fetch_market(market_id):
        fetched_markets.append(market_id)
        return raw

    def fetch_book(token_id):
        fetched_books.append(token_id)
        return {"bids": [], "asks": []}

    summary = track(storage, fetch_market, fetch_book, NOW)

    assert fetched_markets == [raw["id"]]
    assert len(fetched_books) == 1
    _, (point,) = price_points(storage)
    assert [h["arm"] for h in point["holders"]] == ["baseline", "prompt_buy", "mix_or"]
    assert summary.snapshotted == 3


def test_a_rerun_in_the_same_hour_replaces_the_runs_price_file(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_overdue_open")
    store(storage, position(raw["id"]))
    track(storage, markets(raw), lambda token: {"bids": [], "asks": []}, NOW)

    summary = track(storage, markets(raw), lambda token: None, NOW.replace(minute=20))

    _, (point,) = price_points(storage)
    assert point["observed_at"] == NOW.replace(minute=20).isoformat()
    assert summary.snapshotted == 1


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

    assert [r.market_id for r in resolutions(storage)] == [resolved["id"]]
    assert summary.errors == [(broken["id"], "OSError('gamma down')")]


def test_a_failed_book_fetch_still_records_the_price_point(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_overdue_open")
    store(storage, position(raw["id"]))

    def fetch_book(token_id):
        raise BookFetchError("clob down")

    track(storage, markets(raw), fetch_book, NOW)

    _, (point,) = price_points(storage)
    assert point["books"] == {"NO": None}
    assert point["no_price"] == pytest.approx(0.9995)


def test_settling_frees_exposure_in_the_portfolio_that_entry_reads(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_resolved_no")
    store(storage, position(raw["id"]))
    config = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")

    before = load_ledger(storage).portfolio(config, "baseline", "bond")
    track(storage, markets(raw), no_book, NOW)
    after = load_ledger(storage).portfolio(config, "baseline", "bond")

    assert before.exposure_usd == pytest.approx(9.30)
    assert after.exposure_usd == 0.0
    assert after.balance_usd == pytest.approx(1000.0 - 9.30 - 0.05 + 10.0)


def test_a_run_reads_and_writes_the_trades_table_once(tmp_path):
    from counting import CountingStorage

    storage = CountingStorage(tmp_path)
    resolved, still_open = load("settle_resolved_no"), load("settle_overdue_open")
    store(storage, position(resolved["id"]))
    store(storage, position(still_open["id"]))
    storage.reset()

    track(storage, markets(resolved, still_open), lambda token: None, NOW)

    assert storage.calls == {"read_versioned": 1, "write_versioned": 1, "put": 1}
