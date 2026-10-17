from datetime import date

import pytest

from bondhype.ledger import (
    TRADES_KEY,
    Ledger,
    MissingTradesTable,
    decode_ledger,
    encode_ledger,
    load_ledger,
    save_ledger,
)
from bondhype.portfolio import position_key
from bondhype.storage import LocalStorage, StaleWrite
from builders import make_overdue, make_position, make_resolution
from counting import CountingStorage


def ledger_with(*positions):
    ledger = Ledger()
    for position in positions:
        ledger.add_position(position)
    return ledger


def test_a_ledger_round_trips_through_csv_exactly():
    settled = make_position("m1", arm="mix_or", strategy="hype", shares=1 / 3, partial=True)
    overdue = make_position("m2")
    ledger = ledger_with(settled, overdue, make_position("m3"))
    ledger.add_resolution(make_resolution(settled, uma_statuses=("proposed", "disputed")))
    ledger.set_overdue(make_overdue(overdue, uma_status=None), date(2026, 10, 9))

    back = decode_ledger(encode_ledger(ledger))

    assert back.trades == ledger.trades
    assert {m: t.status for (_, _, m), t in back.trades.items()} == {
        "m1": "resolved",
        "m2": "overdue",
        "m3": "open",
    }


def test_ledger_queries_are_per_arm_and_strategy():
    settled = make_position("m1", arm="mix_or", strategy="hype")
    open_ = make_position("m2")
    ledger = ledger_with(settled, open_)
    ledger.add_resolution(make_resolution(settled))

    assert ledger.positions("mix_or", "hype") == [settled]
    assert ledger.resolutions("mix_or", "hype") == [make_resolution(settled)]
    assert ledger.positions("baseline", "bond") == [open_]
    assert ledger.unsettled() == [open_]
    assert ledger.holds("baseline", "bond", "m2")
    assert not ledger.holds("baseline", "hype", "m2")


def test_only_unresolved_trades_report_their_overdue_analysis():
    late, settled_late = make_position("m1"), make_position("m2")
    ledger = ledger_with(late, settled_late)
    for position in (late, settled_late):
        ledger.set_overdue(make_overdue(position), date(2026, 10, 9))
    ledger.add_resolution(make_resolution(settled_late))

    assert ledger.overdue("baseline", "bond") == [make_overdue(late)]


def test_load_and_save_are_one_request_each(tmp_path):
    storage = CountingStorage(tmp_path)
    save_ledger(storage, ledger_with(make_position("m1")))
    storage.reset()

    ledger = load_ledger(storage)
    ledger.add_position(make_position("m2"))
    save_ledger(storage, ledger)

    assert storage.calls == {"read_versioned": 1, "write_versioned": 1}
    assert len(load_ledger(storage).trades) == 2


def test_an_unchanged_ledger_is_not_written(tmp_path):
    storage = CountingStorage(tmp_path)
    save_ledger(storage, ledger_with(make_position("m1")))
    storage.reset()

    save_ledger(storage, load_ledger(storage))

    assert storage.calls["write_versioned"] == 0


def test_a_save_over_a_table_another_job_changed_is_refused(tmp_path):
    storage = LocalStorage(tmp_path)
    save_ledger(storage, ledger_with(make_position("m1")))
    mine, theirs = load_ledger(storage), load_ledger(storage)
    theirs.add_position(make_position("m2"))
    save_ledger(storage, theirs)

    mine.add_position(make_position("m3"))
    with pytest.raises(StaleWrite):
        save_ledger(storage, mine)
    assert set(load_ledger(storage).trades) == {
        ("baseline", "bond", "m1"),
        ("baseline", "bond", "m2"),
    }


def test_a_fresh_store_starts_with_an_empty_ledger(tmp_path):
    assert load_ledger(LocalStorage(tmp_path)).trades == {}


def test_a_store_with_old_per_record_files_but_no_table_fails_closed(tmp_path):
    storage = LocalStorage(tmp_path)
    position = make_position("m1")
    storage.put(position_key("baseline", "bond", "m1"), position.to_json())

    with pytest.raises(MissingTradesTable):
        load_ledger(storage)
    assert not storage.exists(TRADES_KEY)
