from datetime import timedelta

from bondhype.ledger import load_ledger, save_checkpoint
from bondhype.portfolio import position_key
from bondhype.settlement import resolution_key
from builders import NOW, make_position, make_resolution
from counting import CountingStorage


def store_position(storage, position):
    storage.put(
        position_key(position.arm, position.strategy, position.market_id), position.to_json()
    )


def store_resolution(storage, resolution):
    key = resolution_key(resolution.arm, resolution.strategy, resolution.market_id)
    storage.put(key, resolution.to_json())


def test_ledger_holds_every_stored_position_and_resolution_per_arm_and_strategy(tmp_path):
    storage = CountingStorage(tmp_path)
    settled = make_position("m1", arm="mix_or", strategy="hype")
    open_ = make_position("m2")
    store_position(storage, settled)
    store_position(storage, open_)
    store_resolution(storage, make_resolution(settled))

    ledger = load_ledger(storage, NOW)

    assert ledger.positions("mix_or", "hype") == [settled]
    assert ledger.resolutions("mix_or", "hype") == [make_resolution(settled)]
    assert ledger.positions("baseline", "bond") == [open_]
    assert ledger.unsettled() == [open_]
    assert ledger.holds("baseline", "bond", "m2")
    assert not ledger.holds("baseline", "hype", "m2")


def test_a_checkpointed_ledger_reads_only_keys_written_after_the_checkpoint(tmp_path):
    storage = CountingStorage(tmp_path)
    old = [make_position(f"m{i}") for i in range(5)]
    for position in old:
        store_position(storage, position)
    store_resolution(storage, make_resolution(old[0]))
    save_checkpoint(storage, load_ledger(storage, NOW), NOW)

    later = NOW + timedelta(minutes=15)
    new_position = make_position("m9", arm="prompt_buy")
    new_resolution = make_resolution(old[1])
    store_position(storage, new_position)
    store_resolution(storage, new_resolution)
    storage.reset()

    ledger = load_ledger(storage, later)

    rebuilt = load_ledger(CountingStorage(tmp_path), later, use_checkpoint=False)
    assert ledger == rebuilt
    data_reads = [k for k in storage.got if k.startswith("arms/")]
    assert sorted(data_reads) == sorted(
        [
            position_key("prompt_buy", "bond", "m9"),
            resolution_key("baseline", "bond", "m1"),
        ]
    )


def test_a_checkpoint_from_yesterday_is_used_after_midnight(tmp_path):
    storage = CountingStorage(tmp_path)
    for i in range(3):
        store_position(storage, make_position(f"m{i}"))
    evening = NOW.replace(hour=23, minute=45)
    save_checkpoint(storage, load_ledger(storage, evening), evening)
    storage.reset()

    load_ledger(storage, evening + timedelta(minutes=15))

    assert not [k for k in storage.got if k.startswith("arms/")]


def test_a_retried_run_keeps_the_first_checkpoint(tmp_path):
    storage = CountingStorage(tmp_path)
    store_position(storage, make_position("m1"))
    save_checkpoint(storage, load_ledger(storage, NOW), NOW)
    store_position(storage, make_position("m2"))

    save_checkpoint(storage, load_ledger(storage, NOW), NOW)  # must not raise

    assert len(load_ledger(storage, NOW).positions("baseline", "bond")) == 2
