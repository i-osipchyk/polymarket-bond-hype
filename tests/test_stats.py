from pathlib import Path

import pytest

from bondhype.config import load_config
from bondhype.stats import arm_stats, compare_to_baseline, verdict
from builders import make_overdue, make_position, make_resolution

GATE = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml").gate
NO_CAPITAL_COST = GATE.model_copy(update={"capital_cost_annual_rate": 0.0})


def _settled(n_wins, n_losses, **position_kwargs):
    """Positions of 10 shares at 94c + 5c fee; wins pay 10, losses pay 0."""
    positions, resolutions = [], []
    for i in range(n_wins + n_losses):
        position = make_position(f"m{i}", **position_kwargs)
        positions.append(position)
        resolutions.append(make_resolution(position, won=i < n_wins))
    return positions, resolutions


def test_resolved_view_reports_net_ev_win_rate_and_break_even():
    # win: +0.55, loss: -9.45 -> mean (0.55 - 9.45) / 2
    positions, resolutions = _settled(1, 1)

    view = arm_stats(positions, resolutions, [], NO_CAPITAL_COST).resolved

    assert view.n_trades == 2
    assert view.net_ev_usd == pytest.approx(-4.45)
    assert view.win_rate == 0.5
    assert view.break_even_rate == pytest.approx(0.945)  # (9.4 + 0.05) / 10 shares


def test_capital_cost_is_charged_pro_rata_by_days_held():
    # 365-day rate of 365% => 1% a day; 9.45 USD tied up for 5 days costs 0.4725
    gate = GATE.model_copy(update={"capital_cost_annual_rate": 3.65})
    positions, resolutions = _settled(1, 0)

    view = arm_stats(positions, resolutions, [], gate).resolved

    assert view.net_ev_usd == pytest.approx(0.55 - 0.4725)


def test_conservative_view_counts_overdue_positions_as_total_losses():
    won = make_position("won")
    overdue = make_position("overdue")
    waiting = make_position("waiting")  # unresolved but not yet past its end date

    stats = arm_stats(
        [won, overdue, waiting],
        [make_resolution(won)],
        [make_overdue(overdue)],
        NO_CAPITAL_COST,
    )

    assert (stats.resolved.n_trades, stats.resolved.net_ev_usd) == (1, pytest.approx(0.55))
    assert stats.conservative.n_trades == 2
    assert stats.conservative.net_ev_usd == pytest.approx((0.55 - 9.45) / 2)
    assert stats.conservative.win_rate == 0.5
    assert stats.n_overdue == 1


def test_overdue_position_that_later_resolved_is_counted_at_its_real_result():
    position = make_position("late")

    stats = arm_stats(
        [position],
        [make_resolution(position, days_to_resolve=20)],
        [make_overdue(position)],  # stale report from before it resolved
        NO_CAPITAL_COST,
    )

    assert stats.conservative.n_trades == 1
    assert stats.conservative.net_ev_usd == pytest.approx(0.55)
    assert stats.n_overdue == 0


def test_worst_loss_and_max_drawdown_follow_resolution_order():
    # P&L by resolution time: +0.55, -9.45, -9.45, +0.55 -> peak 0.55, trough -18.35
    pnl_days = [("a", True, 1), ("b", False, 2), ("c", False, 3), ("d", True, 4)]
    positions = [make_position(m) for m, _, _ in pnl_days]
    resolutions = [
        make_resolution(p, won=won, days_to_resolve=days)
        for p, (_, won, days) in zip(positions, pnl_days, strict=True)
    ][::-1]  # stored order must not matter

    view = arm_stats(positions, resolutions, [], NO_CAPITAL_COST).resolved

    assert view.worst_loss_usd == pytest.approx(9.45)
    assert view.max_drawdown_usd == pytest.approx(18.9)


def test_overdue_loss_lands_at_the_end_of_the_drawdown_path():
    win, overdue = make_position("win"), make_position("overdue")

    view = arm_stats(
        [win, overdue], [make_resolution(win)], [make_overdue(overdue)], NO_CAPITAL_COST
    ).conservative

    assert view.worst_loss_usd == pytest.approx(9.45)
    assert view.max_drawdown_usd == pytest.approx(9.45)


def test_ev_interval_collapses_when_every_trade_has_the_same_result():
    positions, resolutions = _settled(30, 0)

    low, high = arm_stats(positions, resolutions, [], NO_CAPITAL_COST).resolved.ev_ci_usd

    assert low == pytest.approx(0.55)
    assert high == pytest.approx(0.55)


def test_ev_interval_is_wide_and_straddles_zero_for_a_skewed_payoff():
    # 19 small wins and one large loss: mean +0.05, yet resampling can drop or repeat the loss
    positions, resolutions = _settled(19, 1)

    view = arm_stats(positions, resolutions, [], NO_CAPITAL_COST).resolved
    low, high = view.ev_ci_usd

    assert view.net_ev_usd == pytest.approx((19 * 0.55 - 9.45) / 20)
    assert low < view.net_ev_usd < high
    assert low < 0 < high


def test_ev_interval_is_reproducible_for_a_fixed_seed():
    positions, resolutions = _settled(15, 5)

    first = arm_stats(positions, resolutions, [], GATE).resolved.ev_ci_usd
    second = arm_stats(positions, resolutions, [], GATE).resolved.ev_ci_usd

    assert first == second


def _stats(n_wins, n_losses):
    positions, resolutions = _settled(n_wins, n_losses)
    return arm_stats(positions, resolutions, [], NO_CAPITAL_COST)


def test_arm_that_avoids_the_baselines_losses_beats_it_at_95_percent():
    # 40 wins vs 30 wins + 10 losses: EV +0.55 vs -1.95 per trade
    comparison = compare_to_baseline(_stats(40, 0), _stats(30, 10), GATE)

    assert comparison.ev_diff_usd == pytest.approx(2.5)
    assert comparison.ev_diff_lower_usd > 0
    assert comparison.beats_baseline


def test_arm_with_the_same_trades_as_baseline_does_not_beat_it():
    comparison = compare_to_baseline(_stats(15, 5), _stats(15, 5), GATE)

    assert comparison.ev_diff_usd == pytest.approx(0.0)
    assert comparison.ev_diff_lower_usd < 0
    assert not comparison.beats_baseline


def test_a_lucky_clean_streak_is_not_significant_against_a_baseline_with_one_loss():
    # 20 clean wins vs 19 wins + 1 loss: one loss in 20 is well within noise
    comparison = compare_to_baseline(_stats(20, 0), _stats(19, 1), GATE)

    assert comparison.ev_diff_usd == pytest.approx(0.5)
    assert not comparison.beats_baseline


def test_arm_with_no_trades_never_beats_baseline():
    comparison = compare_to_baseline(_stats(0, 0), _stats(30, 10), GATE)

    assert not comparison.beats_baseline


BALANCE = 1000.0  # gate limits: 10% => 100 USD


def _failed(arm_verdict):
    return {name for name, ok in arm_verdict.checks.items() if not ok}


def test_baseline_with_enough_profitable_trades_passes_every_check():
    result = verdict({"baseline": _stats(100, 0)}, GATE, BALANCE)

    assert result["baseline"].passed
    assert result["baseline"].comparison is None  # baseline is the reference, not compared


def test_too_few_resolved_trades_fails_only_the_trade_count_check():
    result = verdict({"baseline": _stats(99, 0)}, GATE, BALANCE)

    assert _failed(result["baseline"]) == {"min_resolved_trades"}
    assert not result["baseline"].passed


def test_win_rate_must_beat_break_even_by_the_margin():
    # 96 of 100 is 1.5pt above the 94.5% break-even, clearing the 1pt margin
    ok = verdict({"baseline": _stats(96, 4)}, GATE, BALANCE)["baseline"]
    # 94 of 100 is below the 94.5% break-even
    bad = verdict({"baseline": _stats(94, 6)}, GATE, BALANCE)["baseline"]

    assert "win_rate_margin" not in _failed(ok)
    assert "win_rate_margin" in _failed(bad)
    assert "positive_ev" in _failed(bad)


def test_win_rate_above_break_even_but_inside_the_margin_fails():
    # 95.0% observed vs 94.5% + 1pt = 95.5% required
    result = verdict({"baseline": _stats(95, 5)}, GATE, BALANCE)

    assert "win_rate_margin" in _failed(result["baseline"])


def test_single_loss_above_ten_percent_of_balance_fails_the_worst_loss_check():
    big = make_position("big", shares=160.0, filled_usd=150.0, fee_usd=0.0)
    positions, resolutions = _settled(100, 0)
    stats = arm_stats([*positions, big], [*resolutions, make_resolution(big, won=False)], [], GATE)

    failed = _failed(verdict({"baseline": stats}, GATE, BALANCE)["baseline"])

    assert "worst_loss" in failed


def test_drawdown_above_ten_percent_fails_even_when_no_single_loss_does():
    # two 60 USD losses: each within 100, together a 120 drawdown
    positions, resolutions = _settled(100, 0)
    losers = [make_position(f"l{i}", shares=64.0, filled_usd=60.0, fee_usd=0.0) for i in range(2)]
    stats = arm_stats(
        [*positions, *losers],
        [
            *resolutions,
            *(make_resolution(p, won=False, days_to_resolve=6 + i) for i, p in enumerate(losers)),
        ],
        [],
        GATE,
    )

    failed = _failed(verdict({"baseline": stats}, GATE, BALANCE)["baseline"])

    assert "max_drawdown" in failed
    assert "worst_loss" not in failed


def test_gate_uses_the_conservative_view_so_a_large_overdue_position_fails_it():
    positions, resolutions = _settled(100, 0)
    stuck = make_position("stuck", shares=160.0, filled_usd=150.0, fee_usd=0.0)
    stats = arm_stats([*positions, stuck], resolutions, [make_overdue(stuck)], GATE)

    assert stats.resolved.worst_loss_usd == 0.0
    assert "worst_loss" in _failed(verdict({"baseline": stats}, GATE, BALANCE)["baseline"])


def test_llm_arm_passes_only_if_it_also_beats_baseline():
    baseline = _stats(100, 10)  # 110 trades, 9% losing: positive EV not required of the reference
    good = verdict({"baseline": baseline, "prompt_reject": _stats(100, 0)}, GATE, BALANCE)
    same = verdict({"baseline": _stats(100, 0), "prompt_reject": _stats(100, 0)}, GATE, BALANCE)

    assert good["prompt_reject"].passed
    assert good["prompt_reject"].comparison.beats_baseline
    assert _failed(same["prompt_reject"]) == {"beats_baseline"}
    assert not same["prompt_reject"].passed


def test_llm_arm_cannot_pass_without_a_baseline_to_compare_against():
    result = verdict({"prompt_reject": _stats(100, 0)}, GATE, BALANCE)

    assert _failed(result["prompt_reject"]) == {"beats_baseline"}
