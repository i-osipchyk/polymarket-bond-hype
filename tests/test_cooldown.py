from datetime import timedelta
from pathlib import Path

import pytest

from bondhype.config import load_config
from bondhype.cooldown import cooldown_active, record_rejection
from bondhype.storage import LocalStorage
from builders import NOW

CONFIG = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")


def active(storage, price, arm="prompt_buy", strategy="bond", market_id="m1"):
    return cooldown_active(storage, CONFIG, arm, strategy, market_id, price)


def test_market_is_cooling_down_right_after_a_rejection_but_not_before(tmp_path):
    storage = LocalStorage(tmp_path)
    assert not active(storage, 0.93)

    record_rejection(storage, "prompt_buy", "bond", "m1", price=0.93, now=NOW)

    assert active(storage, 0.93)


def test_cooldown_is_per_arm_strategy_and_market(tmp_path):
    storage = LocalStorage(tmp_path)
    record_rejection(storage, "prompt_buy", "bond", "m1", price=0.93, now=NOW)

    assert not active(storage, 0.93, arm="prompt_reject")
    assert not active(storage, 0.93, strategy="hype")
    assert not active(storage, 0.93, market_id="m2")


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
def test_cooldown_ends_only_when_the_price_has_moved_at_least_3_cents(tmp_path, price, expected):
    storage = LocalStorage(tmp_path)
    record_rejection(storage, "prompt_buy", "bond", "m1", price=0.93, now=NOW)

    assert active(storage, price) is expected


def test_a_later_rejection_moves_the_reference_price(tmp_path):
    storage = LocalStorage(tmp_path)
    record_rejection(storage, "prompt_buy", "bond", "m1", price=0.93, now=NOW)
    second = NOW + timedelta(hours=2)
    assert not active(storage, 0.90)  # moved 3c, re-evaluated

    record_rejection(storage, "prompt_buy", "bond", "m1", price=0.90, now=second)

    assert active(storage, 0.92)  # 2c from the new reference
    assert not active(storage, 0.87)
