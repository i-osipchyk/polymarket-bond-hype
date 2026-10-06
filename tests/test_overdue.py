import json
from datetime import UTC, datetime

import pytest

from bondhype.overdue import run_overdue
from bondhype.portfolio import position_key
from bondhype.storage import LocalStorage
from test_settlement import load, position

NOW = datetime(2026, 10, 3, 3, 59, tzinfo=UTC)  # two days after the fixture's end date


def store(storage, pos):
    storage.put(position_key(pos.arm, pos.strategy, pos.market_id), pos.to_json())


def markets(*raws):
    by_id = {raw["id"]: raw for raw in raws}
    return lambda market_id: by_id[market_id]


def test_overdue_position_gets_one_analysis_record_per_day(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_overdue_open")
    store(storage, position(raw["id"], arm="prompt_buy", strategy="bond"))

    for _ in range(2):  # a retried run writes the same record
        run_overdue(storage, markets(raw), NOW)

    (key,) = storage.list("arms/prompt_buy/bond/overdue/")
    assert key == f"arms/prompt_buy/bond/overdue/{raw['id']}/2026-10-03.json"
    record = json.loads(storage.get(key))
    assert record["days_overdue"] == pytest.approx(2.0)
    assert record["uma_status"] is None
    assert record["held_side_price"] == pytest.approx(0.9995)
    assert record["conservative_pnl_usd"] == pytest.approx(-9.35)
    assert record["market_id"] == raw["id"]


def test_positions_not_yet_due_or_already_settled_get_no_record(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_overdue_open")
    resolved = load("settle_resolved_no")
    store(storage, position(raw["id"]))
    store(storage, position(resolved["id"]))
    before_end = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)

    run_overdue(storage, markets(raw, resolved), before_end)
    run_overdue(storage, markets(raw, resolved), NOW)

    assert storage.list("arms/baseline/bond/overdue/") == [
        f"arms/baseline/bond/overdue/{raw['id']}/2026-10-03.json"
    ]


def test_a_failed_fetch_does_not_block_other_overdue_positions(tmp_path):
    storage = LocalStorage(tmp_path)
    broken = load("settle_disputed")
    raw = load("settle_overdue_open")
    store(storage, position(broken["id"]))
    store(storage, position(raw["id"]))

    def fetch_market(market_id):
        if market_id == broken["id"]:
            raise OSError("gamma down")
        return raw

    errors = run_overdue(storage, fetch_market, NOW)

    assert errors == [(broken["id"], "OSError('gamma down')")]
    assert len(storage.list("arms/baseline/bond/overdue/")) == 1
