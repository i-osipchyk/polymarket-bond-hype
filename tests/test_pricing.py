from datetime import UTC, datetime, timezone
from pathlib import Path

import pytest

from bondhype.llm import Usage
from bondhype.pricing import UnknownModel, call_cost, load_pricing

PRICING_FILE = Path(__file__).parent.parent / "deepseek_pricing.yaml"
MILLION_EACH = Usage(
    cache_hit_tokens=1_000_000, cache_miss_tokens=1_000_000, output_tokens=1_000_000
)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 8, hour, minute, tzinfo=UTC)


@pytest.fixture(scope="module")
def pricing():
    return load_pricing(PRICING_FILE)


def test_an_off_peak_call_is_priced_per_million_tokens_at_the_off_peak_rates(pricing):
    assert call_cost(MILLION_EACH, "deepseek-flash", at(12), pricing) == pytest.approx(0.753)


def test_a_peak_call_is_priced_at_the_peak_rates(pricing):
    assert call_cost(MILLION_EACH, "deepseek-flash", at(2), pricing) == pytest.approx(1.506)


def test_the_pro_model_has_its_own_rates(pricing):
    assert call_cost(MILLION_EACH, "deepseek-v4-pro", at(12), pricing) == pytest.approx(2.662)


def test_only_the_tokens_used_are_charged(pricing):
    usage = Usage(cache_hit_tokens=350, cache_miss_tokens=150, output_tokens=80)

    # (350 * 0.003 + 150 * 0.15 + 80 * 0.60) / 1e6
    assert call_cost(usage, "deepseek-flash", at(12), pricing) == pytest.approx(0.00007155)


@pytest.mark.parametrize(
    ("hour", "minute", "peak"),
    [
        (0, 59, False),
        (1, 0, True),  # a window includes its start
        (3, 59, True),
        (4, 0, False),  # and excludes its end
        (5, 59, False),
        (6, 0, True),
        (9, 59, True),
        (10, 0, False),
        (23, 59, False),
    ],
)
def test_peak_windows_include_their_start_and_exclude_their_end(pricing, hour, minute, peak):
    cost = call_cost(MILLION_EACH, "deepseek-flash", at(hour, minute), pricing)

    assert cost == pytest.approx(1.506 if peak else 0.753)


def test_the_window_is_judged_in_utc_whatever_the_zone_of_the_timestamp(pricing):
    from datetime import timedelta

    plus_two = timezone(timedelta(hours=2))
    local = datetime(2026, 10, 8, 4, 30, tzinfo=plus_two)  # 02:30 UTC, peak

    assert call_cost(MILLION_EACH, "deepseek-flash", local, pricing) == pytest.approx(1.506)


def test_a_model_without_a_price_is_an_error_not_a_free_call(pricing):
    with pytest.raises(UnknownModel):
        call_cost(MILLION_EACH, "deepseek-mystery", at(12), pricing)
