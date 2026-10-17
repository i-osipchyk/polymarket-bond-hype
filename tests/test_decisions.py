from datetime import timedelta
from pathlib import Path

import pytest

from bondhype.config import load_config
from bondhype.decisions import (
    Cooldowns,
    DecisionLog,
    decode_decisions,
    encode_decisions,
    load_decisions,
    save_decisions,
)
from bondhype.storage import LocalStorage, StaleWrite
from builders import NOW, make_decision
from counting import CountingStorage

CONFIG = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")


def active(decisions, price, arm="prompt_buy", strategy="bond", market_id="m1"):
    return Cooldowns(decisions).active(CONFIG, arm, strategy, market_id, price)


def test_decisions_round_trip_through_csv_exactly():
    decisions = [
        make_decision("m1"),
        make_decision("m2", outcomes={"prompt_buy": "refused:event_cap", "mix_or": "opened"}),
    ]
    assert decode_decisions(encode_decisions(decisions)) == decisions


def test_market_is_cooling_down_right_after_a_rejection_but_not_before():
    assert not active([], 0.93)
    assert active([make_decision(ask=0.93)], 0.93)


def test_a_refused_entry_cools_down_like_a_rejection():
    assert active([make_decision(outcomes={"prompt_buy": "refused:event_cap"})], 0.93)


@pytest.mark.parametrize("outcome", ["opened", "held", "cooldown"])
def test_other_outcomes_start_no_cooldown(outcome):
    assert not active([make_decision(outcomes={"prompt_buy": outcome})], 0.93)


def test_cooldown_is_per_arm_strategy_and_market():
    decisions = [make_decision(ask=0.93)]
    assert not active(decisions, 0.93, arm="prompt_reject")
    assert not active(decisions, 0.93, strategy="hype")
    assert not active(decisions, 0.93, market_id="m2")


@pytest.mark.parametrize(
    "price,expected",
    [
        pytest.param(0.93, True, id="same-price"),
        pytest.param(0.94, True, id="1c-up"),
        pytest.param(0.91, True, id="2c-down"),
        pytest.param(0.9201, True, id="just-under-3c-down"),
        pytest.param(0.96, False, id="exactly-3c-up-ends-cooldown"),
        pytest.param(0.90, False, id="exactly-3c-down-ends-cooldown"),
        pytest.param(0.97, False, id="4c-up"),
        pytest.param(0.80, False, id="13c-down"),
    ],
)
def test_cooldown_ends_only_when_the_price_has_moved_at_least_3_cents(price, expected):
    assert active([make_decision(ask=0.93)], price) is expected


def test_a_later_rejection_moves_the_reference_price():
    first = make_decision(ask=0.93)
    second = make_decision(ask=0.90, scan_at=NOW + timedelta(hours=2))
    assert not active([first], 0.90)  # moved 3c, re-evaluated

    assert active([second, first], 0.92)  # 2c from the new reference, whatever the row order
    assert not active([first, second], 0.87)


def test_a_retried_scan_drops_the_rows_its_earlier_attempt_left():
    log = DecisionLog([make_decision("m1", scan_at=NOW - timedelta(hours=1)), make_decision("m2")])

    log.begin_scan(NOW)

    assert [d.market_id for d in log.decisions] == ["m1"]
    assert log.changed


def test_load_and_save_are_one_request_each(tmp_path):
    storage = CountingStorage(tmp_path)
    save_decisions(storage, DecisionLog([make_decision("m1")], changed=True))
    storage.reset()

    log = load_decisions(storage)
    log.add(make_decision("m2"))
    save_decisions(storage, log)

    assert storage.calls == {"read_versioned": 1, "write_versioned": 1}
    assert len(load_decisions(storage).decisions) == 2


def test_a_save_over_a_table_another_run_changed_is_refused(tmp_path):
    storage = LocalStorage(tmp_path)
    mine, theirs = load_decisions(storage), load_decisions(storage)
    theirs.add(make_decision("m1"))
    save_decisions(storage, theirs)

    mine.add(make_decision("m2"))
    with pytest.raises(StaleWrite):
        save_decisions(storage, mine)
