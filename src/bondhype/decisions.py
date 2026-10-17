"""Every LLM-reviewed candidate in one table, `state/decisions.csv`, read and written once per scan.

One row per scan, strategy and market sent to the LLM: both prompts' verdict, flags, confidence,
reason and cost, and what each LLM arm then did. Cooldowns are derived from it: an arm that
rejected a market, or whose entry the portfolio refused, keeps it out until the ask moves at least
`cooldown_price_move` from the ask at that row. Full prompts and replies go to the write-only
`llm_calls/` audit files, not here.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from bondhype import csvtable as c
from bondhype.config import Config
from bondhype.llm import Verdict
from bondhype.storage import Storage

DECISIONS_KEY = "state/decisions.csv"
LLM_ARMS = ("prompt_reject", "prompt_buy", "mix_and", "mix_or")

# What an LLM arm did with a reviewed candidate.
OPENED, REJECTED, HELD, COOLDOWN = "opened", "rejected", "held", "cooldown"
REFUSED = "refused:"  # followed by the entry rejection, e.g. "refused:event_cap"

PROMPT_FIELDS = ("prompt", "verdict", "risk_flags", "confidence", "reason", "cost_usd")
COLUMNS = (
    "scan_at",
    "config_version",
    "market_id",
    "strategy",
    "side",
    "ask",
    "question",
    "model",
    *(f"buy_{f}" for f in PROMPT_FIELDS),
    *(f"reject_{f}" for f in PROMPT_FIELDS),
    *LLM_ARMS,
)


@dataclass(frozen=True)
class Review:
    """One prompt's result for one candidate; cost is None when unknown or not called."""

    prompt_id: str
    verdict: Verdict
    cost_usd: float | None


@dataclass
class Decision:
    scan_at: datetime
    config_version: str
    market_id: str
    strategy: str
    side: str
    ask: float
    question: str
    model: str
    buy: Review
    reject: Review
    outcomes: dict[str, str] = field(default_factory=dict)  # arm -> outcome, LLM arms only


def is_rejection(outcome: str) -> bool:
    return outcome == REJECTED or outcome.startswith(REFUSED)


def _prompt_cells(prefix: str, review: Review) -> dict[str, str]:
    v = review.verdict
    return {
        f"{prefix}_prompt": review.prompt_id,
        f"{prefix}_verdict": v.verdict,
        f"{prefix}_risk_flags": c.items(v.risk_flags),
        f"{prefix}_confidence": c.number(v.confidence),
        f"{prefix}_reason": v.reason,
        f"{prefix}_cost_usd": c.number(review.cost_usd),
    }


def _review(prefix: str, row: dict[str, str]) -> Review:
    return Review(
        prompt_id=row[f"{prefix}_prompt"],
        verdict=Verdict(
            verdict=row[f"{prefix}_verdict"],
            risk_flags=c.to_items(row[f"{prefix}_risk_flags"]),
            confidence=c.opt_int(row[f"{prefix}_confidence"]),
            reason=row[f"{prefix}_reason"],
        ),
        cost_usd=c.opt_float(row[f"{prefix}_cost_usd"]),
    )


def _row(d: Decision) -> dict[str, str]:
    return {
        "scan_at": c.moment(d.scan_at),
        "config_version": d.config_version,
        "market_id": d.market_id,
        "strategy": d.strategy,
        "side": d.side,
        "ask": c.number(d.ask),
        "question": d.question,
        "model": d.model,
        **_prompt_cells("buy", d.buy),
        **_prompt_cells("reject", d.reject),
        **{arm: d.outcomes.get(arm, "") for arm in LLM_ARMS},
    }


def _decision(row: dict[str, str]) -> Decision:
    return Decision(
        scan_at=c.to_datetime(row["scan_at"]),
        config_version=row["config_version"],
        market_id=row["market_id"],
        strategy=row["strategy"],
        side=row["side"],
        ask=c.to_float(row["ask"]),
        question=row["question"],
        model=row["model"],
        buy=_review("buy", row),
        reject=_review("reject", row),
        outcomes={arm: row[arm] for arm in LLM_ARMS if row[arm]},
    )


def encode_decisions(decisions: Iterable[Decision]) -> bytes:
    ordered = sorted(decisions, key=lambda d: (d.scan_at, d.strategy, d.market_id))
    return c.encode(COLUMNS, (_row(d) for d in ordered))


def decode_decisions(data: bytes) -> list[Decision]:
    return [_decision(row) for row in c.decode(COLUMNS, data)]


@dataclass
class DecisionLog:
    decisions: list[Decision] = field(default_factory=list)
    version: str | None = None  # the stored version this was read at; None: no table yet
    changed: bool = False

    def begin_scan(self, now: datetime) -> None:
        """A retried scan redoes its scan time, so rows an earlier attempt left are dropped."""
        kept = [d for d in self.decisions if d.scan_at != now]
        if len(kept) != len(self.decisions):
            self.decisions, self.changed = kept, True

    def add(self, decision: Decision) -> None:
        self.decisions.append(decision)
        self.changed = True

    def on(self, day: datetime) -> list[Decision]:
        return [d for d in self.decisions if d.scan_at.date() == day.date()]


def load_decisions(storage: Storage) -> DecisionLog:
    stored = storage.read_versioned(DECISIONS_KEY)
    if stored.data is None:
        return DecisionLog()
    return DecisionLog(decode_decisions(stored.data), stored.version)


def save_decisions(storage: Storage, log: DecisionLog) -> None:
    """One conditional write, skipped when nothing changed."""
    if not log.changed:
        return
    data = encode_decisions(log.decisions)
    log.version = storage.write_versioned(DECISIONS_KEY, data, log.version)
    log.changed = False


class Cooldowns:
    """Ask at each arm's latest rejection of a market, from the decisions already loaded."""

    def __init__(self, decisions: Iterable[Decision]):
        self._price: dict[tuple[str, str, str], float] = {}
        for d in sorted(decisions, key=lambda d: d.scan_at):
            for arm, outcome in d.outcomes.items():
                if is_rejection(outcome):
                    self._price[(arm, d.strategy, d.market_id)] = d.ask

    def active(self, config: Config, arm: str, strategy: str, market_id: str, price: float) -> bool:
        """A rejected market stays out until its price is at least `cooldown_price_move` away from
        the price at the last rejection, either way. Time alone never ends a cooldown."""
        last = self._price.get((arm, strategy, market_id))
        if last is None:
            return False
        return round(abs(price - last), 6) < config.portfolio.cooldown_price_move
