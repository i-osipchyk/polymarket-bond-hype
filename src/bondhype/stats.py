"""Gate statistics and verdict: pure functions over stored positions, resolutions, overdue."""

import random
from dataclasses import dataclass
from datetime import datetime

from bondhype.config import GateRules
from bondhype.portfolio import PositionOpened
from bondhype.settlement import OverdueReport, Resolution


@dataclass(frozen=True)
class _Trade:
    pnl_usd: float
    won: bool
    break_even_rate: float
    resolved_at: datetime | None  # None: overdue, ordered after every real resolution


@dataclass(frozen=True)
class ViewStats:
    n_trades: int
    net_ev_usd: float
    win_rate: float
    break_even_rate: float
    ev_ci_usd: tuple[float, float]
    worst_loss_usd: float
    max_drawdown_usd: float


@dataclass(frozen=True)
class ArmStats:
    resolved: ViewStats
    conservative: ViewStats
    n_overdue: int
    conservative_pnls_usd: tuple[float, ...]


def _max_drawdown(pnls: list[float]) -> float:
    total = peak = drawdown = 0.0
    for pnl in pnls:
        total += pnl
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
    return drawdown


def _bootstrap_means(values: list[float], gate: GateRules, seed_offset: int = 0) -> list[float]:
    rng = random.Random(gate.bootstrap_seed + seed_offset)
    n = len(values)
    return [sum(rng.choices(values, k=n)) / n for _ in range(gate.bootstrap_samples)]


def _quantile(sorted_values: list[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def _view(trades: list[_Trade], gate: GateRules) -> ViewStats:
    n = len(trades)
    if n == 0:
        return ViewStats(0, 0.0, 0.0, 0.0, (0.0, 0.0), 0.0, 0.0)
    ordered = sorted(trades, key=lambda t: (t.resolved_at is None, t.resolved_at or datetime.min))
    pnls = [t.pnl_usd for t in ordered]
    means = sorted(_bootstrap_means(pnls, gate))
    return ViewStats(
        n_trades=n,
        net_ev_usd=sum(pnls) / n,
        win_rate=sum(t.won for t in trades) / n,
        break_even_rate=sum(t.break_even_rate for t in trades) / n,
        ev_ci_usd=(
            _quantile(means, (1 - gate.confidence) / 2),
            _quantile(means, 1 - (1 - gate.confidence) / 2),
        ),
        worst_loss_usd=max(0.0, -min(pnls)),
        max_drawdown_usd=_max_drawdown(pnls),
    )


def arm_stats(
    positions: list[PositionOpened],
    resolutions: list[Resolution],
    overdue: list[OverdueReport],
    gate: GateRules,
) -> ArmStats:
    by_market = {p.market_id: p for p in positions}
    resolved = []
    for r in resolutions:
        p = by_market[r.market_id]
        cost_usd = p.filled_usd + p.fee_usd
        days_held = (r.resolved_at - p.opened_at).total_seconds() / 86400
        capital_cost = cost_usd * gate.capital_cost_annual_rate * days_held / 365
        resolved.append(
            _Trade(
                pnl_usd=r.pnl_usd - capital_cost,
                won=r.payout_usd > 0,
                break_even_rate=cost_usd / p.shares,
                resolved_at=r.resolved_at,
            )
        )
    resolved_markets = {r.market_id for r in resolutions}
    overdue_losses = {
        o.market_id: o
        for o in overdue
        if o.market_id in by_market and o.market_id not in resolved_markets
    }
    lost = [
        _Trade(
            pnl_usd=o.conservative_pnl_usd,
            won=False,
            break_even_rate=(by_market[m].filled_usd + by_market[m].fee_usd) / by_market[m].shares,
            resolved_at=None,
        )
        for m, o in overdue_losses.items()
    ]
    return ArmStats(
        resolved=_view(resolved, gate),
        conservative=_view(resolved + lost, gate),
        n_overdue=len(lost),
        conservative_pnls_usd=tuple(t.pnl_usd for t in resolved + lost),
    )


@dataclass(frozen=True)
class Comparison:
    ev_diff_usd: float
    ev_diff_lower_usd: float  # one-sided bound at the gate confidence
    beats_baseline: bool


def compare_to_baseline(arm: ArmStats, baseline: ArmStats, gate: GateRules) -> Comparison:
    """Net EV per trade, arm minus baseline (conservative view). Independent bootstrap of each arm.

    The arm trades a subset of baseline's markets, so ignoring the pairing makes this conservative.
    """
    arm_pnls, base_pnls = arm.conservative_pnls_usd, baseline.conservative_pnls_usd
    if not arm_pnls or not base_pnls:
        return Comparison(0.0, 0.0, False)
    arm_means = _bootstrap_means(list(arm_pnls), gate, seed_offset=1)
    base_means = _bootstrap_means(list(base_pnls), gate, seed_offset=2)
    diffs = sorted(a - b for a, b in zip(arm_means, base_means, strict=True))
    lower = _quantile(diffs, 1 - gate.confidence)
    return Comparison(
        ev_diff_usd=sum(arm_pnls) / len(arm_pnls) - sum(base_pnls) / len(base_pnls),
        ev_diff_lower_usd=lower,
        beats_baseline=lower > 0,
    )


@dataclass(frozen=True)
class ArmVerdict:
    checks: dict[str, bool]
    passed: bool
    comparison: Comparison | None  # primary comparison vs baseline; None for baseline itself


def verdict(
    stats_by_arm: dict[str, ArmStats], gate: GateRules, starting_balance_usd: float
) -> dict[str, ArmVerdict]:
    """Go-live verdict per arm for ONE strategy. The only place the gate is decided.

    Checks run on the conservative view (overdue = total loss). The only comparisons computed are
    the primary ones, each LLM arm against baseline; any other cut is exploratory and lives in
    reporting, never here.
    """
    limit_usd = gate.max_loss_fraction * starting_balance_usd
    baseline = stats_by_arm.get("baseline")
    verdicts = {}
    for arm, stats in stats_by_arm.items():
        view = stats.conservative
        checks = {
            "min_resolved_trades": stats.resolved.n_trades >= gate.min_resolved_trades,
            "positive_ev": view.net_ev_usd > 0,
            "win_rate_margin": view.win_rate >= view.break_even_rate + gate.win_rate_margin,
            "worst_loss": view.worst_loss_usd <= limit_usd,
            "max_drawdown": view.max_drawdown_usd <= limit_usd,
        }
        comparison = None
        if arm != "baseline":
            comparison = compare_to_baseline(stats, baseline, gate) if baseline else None
            checks["beats_baseline"] = comparison is not None and comparison.beats_baseline
        verdicts[arm] = ArmVerdict(checks, all(checks.values()), comparison)
    return verdicts
