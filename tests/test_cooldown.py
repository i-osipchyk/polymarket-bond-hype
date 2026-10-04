from datetime import timedelta
from pathlib import Path

import pytest

from bondhype.config import load_config
from bondhype.cooldown import cooldown_active, record_rejection
from bondhype.storage import LocalStorage
from builders import NOW

CONFIG = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")


def active(storage, price, now, arm="prompt_buy", strategy="bond", market_id="m1"):
    return cooldown_active(storage, CONFIG, arm, strategy, market_id, price, now)


def test_market_is_cooling_down_right_after_a_rejection_but_not_before(tmp_path):
    storage = LocalStorage(tmp_path)
    assert not active(storage, 0.93, NOW)

    record_rejection(storage, "prompt_buy", "bond", "m1", price=0.93, now=NOW)

    assert active(storage, 0.93, NOW + timedelta(minutes=15))


def test_cooldown_is_per_arm_strategy_and_market(tmp_path):
    storage = LocalStorage(tmp_path)
    record_rejection(storage, "prompt_buy", "bond", "m1", price=0.93, now=NOW)
    later = NOW + timedelta(minutes=15)

    assert not active(storage, 0.93, later, arm="prompt_reject")
    assert not active(storage, 0.93, later, strategy="hype")
    assert not active(storage, 0.93, later, market_id="m2")


@pytest.mark.parametrize(
    "hours,price,expected",
    [
        pytest.param(23, 0.93, True, id="23h-same-price"),
        pytest.param(24, 0.93, False, id="24h-expires"),
        pytest.param(25, 0.93, False, id="25h"),
        pytest.param(1, 0.96, True, id="exactly-3c-up-still-cooling"),
        pytest.param(1, 0.90, True, id="exactly-3c-down-still-cooling"),
        pytest.param(1, 0.97, False, id="4c-up-ends-cooldown"),
        pytest.param(1, 0.89, False, id="4c-down-ends-cooldown"),
    ],
)
def test_cooldown_ends_after_24_hours_or_a_price_move_over_3_cents(
    tmp_path, hours, price, expected
):
    storage = LocalStorage(tmp_path)
    record_rejection(storage, "prompt_buy", "bond", "m1", price=0.93, now=NOW)

    assert active(storage, price, NOW + timedelta(hours=hours)) is expected


def test_a_later_rejection_restarts_the_clock(tmp_path):
    storage = LocalStorage(tmp_path)
    record_rejection(storage, "prompt_buy", "bond", "m1", price=0.93, now=NOW)
    second = NOW + timedelta(hours=30)
    assert not active(storage, 0.93, second)  # first cooldown long over, re-evaluated

    record_rejection(storage, "prompt_buy", "bond", "m1", price=0.93, now=second)

    assert active(storage, 0.93, second + timedelta(hours=1))
    assert not active(storage, 0.93, second + timedelta(hours=24))
