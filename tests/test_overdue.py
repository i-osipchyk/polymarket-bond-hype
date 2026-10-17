from datetime import UTC, date, datetime

import pytest

from bondhype.ledger import load_ledger, save_ledger
from bondhype.overdue import run_overdue
from bondhype.storage import LocalStorage
from test_settlement import load, position

NOW = datetime(2026, 10, 3, 3, 59, tzinfo=UTC)  # two days after the fixture's end date


def store(storage, pos):
    ledger = load_ledger(storage)
    ledger.add_position(pos)
    save_ledger(storage, ledger)


def overdue(storage, arm="baseline", strategy="bond"):
    return load_ledger(storage).overdue(arm, strategy)


def markets(*raws):
    by_id = {raw["id"]: raw for raw in raws}
    return lambda market_id: by_id[market_id]


def test_an_overdue_trade_carries_its_latest_analysis(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_overdue_open")
    store(storage, position(raw["id"], arm="prompt_buy", strategy="bond"))

    for _ in range(2):  # a retried run writes the same analysis
        run_overdue(storage, markets(raw), NOW)

    (record,) = overdue(storage, "prompt_buy")
    trade = load_ledger(storage).trades[("prompt_buy", "bond", raw["id"])]
    assert (trade.status, trade.overdue_checked_on) == ("overdue", date(2026, 10, 3))
    assert record.days_overdue == pytest.approx(2.0)
    assert record.uma_status is None
    assert record.held_side_price == pytest.approx(0.9995)
    assert record.conservative_pnl_usd == pytest.approx(-9.35)
    assert record.market_id == raw["id"]


def test_positions_not_yet_due_or_already_settled_get_no_analysis(tmp_path):
    storage = LocalStorage(tmp_path)
    raw = load("settle_overdue_open")
    resolved = load("settle_resolved_no")
    store(storage, position(raw["id"]))
    store(storage, position(resolved["id"]))
    before_end = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)

    run_overdue(storage, markets(raw, resolved), before_end)
    run_overdue(storage, markets(raw, resolved), NOW)

    assert [o.market_id for o in overdue(storage)] == [raw["id"]]


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
    assert len(overdue(storage)) == 1
