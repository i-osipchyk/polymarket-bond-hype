"""Daily per-arm report and health checks, built from stored files only."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from bondhype.arms import ARMS
from bondhype.config import Config
from bondhype.portfolio import PositionOpened, positions_prefix, resolutions_prefix
from bondhype.settlement import STRATEGIES, OverdueReport, Resolution
from bondhype.stats import ArmStats, ArmVerdict, arm_stats, verdict
from bondhype.storage import Storage


@dataclass(frozen=True)
class ArmReport:
    arm: str
    strategy: str
    candidates_today: int
    trades_today: int
    resolutions_today: int
    llm_error_rate: float | None  # None: no LLM calls today
    overdue: tuple[OverdueReport, ...]
    stats: ArmStats
    verdict: ArmVerdict


@dataclass(frozen=True)
class DailyReport:
    now: datetime
    config_version: str
    entries: tuple[ArmReport, ...]

    def entry(self, arm: str, strategy: str) -> ArmReport:
        return next(e for e in self.entries if (e.arm, e.strategy) == (arm, strategy))


def _candidates_today(storage: Storage, now: datetime) -> dict[str, int]:
    counts = dict.fromkeys(STRATEGIES, 0)
    for key in storage.list(f"candidates/date={now:%Y-%m-%d}/"):
        for line in storage.get(key).decode().splitlines():
            selected = json.loads(line).get("selected")
            if selected:
                counts[selected["strategy"]] += 1
    return counts


def _llm_outcomes_today(storage: Storage, now: datetime) -> dict[tuple[str, str], list[bool]]:
    """Per (prompt id, strategy): whether each of today's calls ended in an error."""
    outcomes: dict[tuple[str, str], list[bool]] = {}
    for key in storage.list(f"llm_calls/date={now:%Y-%m-%d}/"):
        record = json.loads(storage.get(key))
        strategy = json.loads(record["user"])["strategy"]
        outcomes.setdefault((record["prompt_id"], strategy), []).append(
            record["verdict"]["verdict"] == "error"
        )
    return outcomes


def _arm_prompts(arm: str, config: Config) -> tuple[str, ...]:
    return {
        "prompt_reject": (config.llm.reject_prompt,),
        "prompt_buy": (config.llm.buy_prompt,),
        "mix_and": (config.llm.reject_prompt, config.llm.buy_prompt),
        "mix_or": (config.llm.reject_prompt, config.llm.buy_prompt),
    }.get(arm, ())


def _error_rate(outcomes: list[bool]) -> float | None:
    return sum(outcomes) / len(outcomes) if outcomes else None


def _latest_overdue(
    storage: Storage, arm: str, strategy: str, resolved: set[str]
) -> list[OverdueReport]:
    """Newest daily analysis per still-unresolved market (keys sort by market, then date)."""
    latest: dict[str, OverdueReport] = {}
    for key in storage.list(f"arms/{arm}/{strategy}/overdue/"):
        report = OverdueReport.from_json(storage.get(key))
        if report.market_id not in resolved:
            latest[report.market_id] = report
    return list(latest.values())


def build_report(storage: Storage, config: Config, now: datetime) -> DailyReport:
    candidates = _candidates_today(storage, now)
    llm_outcomes = _llm_outcomes_today(storage, now)
    entries = []
    for strategy in STRATEGIES:
        positions, resolutions, overdue, stats = {}, {}, {}, {}
        for arm in ARMS:
            positions[arm] = [
                PositionOpened.from_json(storage.get(key))
                for key in storage.list(positions_prefix(arm, strategy))
            ]
            resolutions[arm] = [
                Resolution.from_json(storage.get(key))
                for key in storage.list(resolutions_prefix(arm, strategy))
            ]
            overdue[arm] = _latest_overdue(
                storage, arm, strategy, {r.market_id for r in resolutions[arm]}
            )
            stats[arm] = arm_stats(positions[arm], resolutions[arm], overdue[arm], config.gate)
        verdicts = verdict(stats, config.gate, config.portfolio.starting_balance_usd)
        for arm in ARMS:
            entries.append(
                ArmReport(
                    arm=arm,
                    strategy=strategy,
                    candidates_today=candidates[strategy],
                    trades_today=sum(p.opened_at.date() == now.date() for p in positions[arm]),
                    resolutions_today=sum(
                        r.resolved_at.date() == now.date() for r in resolutions[arm]
                    ),
                    llm_error_rate=_error_rate(
                        [
                            failed
                            for prompt in _arm_prompts(arm, config)
                            for failed in llm_outcomes.get((prompt, strategy), [])
                        ]
                    ),
                    overdue=tuple(overdue[arm]),
                    stats=stats[arm],
                    verdict=verdicts[arm],
                )
            )
    return DailyReport(now=now, config_version=config.version, entries=tuple(entries))


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _arm_line(e: ArmReport) -> str:
    resolved, conservative = e.stats.resolved, e.stats.conservative
    low, high = resolved.ev_ci_usd
    return (
        f"{e.arm:<14} cand {e.candidates_today} trades {e.trades_today} res {e.resolutions_today}"
        f" | resolved n={resolved.n_trades} EV {resolved.net_ev_usd:+.2f} [{low:+.2f}, {high:+.2f}]"
        f" win {_pct(resolved.win_rate)} vs break-even {_pct(resolved.break_even_rate)}"
        f" | conservative n={conservative.n_trades} EV {conservative.net_ev_usd:+.2f}"
        f" dd {conservative.max_drawdown_usd:.2f} worst {conservative.worst_loss_usd:.2f}"
        f" | llm err {_pct(e.llm_error_rate)}"
        f" | gate {'PASS' if e.verdict.passed else 'FAIL'}"
    )


def format_report(report: DailyReport) -> str:
    lines = [
        f"Daily report {report.now:%Y-%m-%d} (config {report.config_version})",
        "Primary: each arm's gate verdict (LLM arms vs baseline). All other cuts are exploratory.",
    ]
    for strategy in STRATEGIES:
        lines += ["", f"== {strategy.upper()} =="]
        for e in (e for e in report.entries if e.strategy == strategy):
            lines.append(_arm_line(e))
            for o in e.overdue:
                status = "disputed" if o.was_disputed else o.uma_status
                lines.append(f"  overdue {o.market_id} {o.days_overdue:.1f}d {status} ({e.arm})")
    return "\n".join(lines)


@dataclass(frozen=True)
class Heartbeat:
    ok: bool
    message: str


def _scan_time(key: str) -> datetime:
    stamp = key.rsplit("scan=", 1)[1].removesuffix(".jsonl")
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def check_heartbeat(storage: Storage, config: Config, now: datetime) -> Heartbeat:
    """Dead man's switch: every scan writes a candidates file, so silence means a missing scan."""
    keys = storage.list("candidates/")
    if not keys:
        return Heartbeat(False, "No scan output found at all.")
    age_minutes = (now - max(_scan_time(k) for k in keys)).total_seconds() / 60
    if age_minutes > config.health.heartbeat_max_age_minutes:
        return Heartbeat(False, f"No scan output for {age_minutes:.0f} min.")
    return Heartbeat(True, f"Last scan {age_minutes:.0f} min ago.")
