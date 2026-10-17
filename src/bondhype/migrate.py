"""One-off: build state/trades.csv and state/decisions.csv from the per-record files.

The old layout kept one object per position, resolution, overdue analysis, cooldown and LLM call.
This reads all of them once and writes the two tables. The old files are left where they are.
By default the tables go to a local directory for checking; `--write` puts them in the bucket,
and refuses if either table already exists there.
"""

import argparse
import json
import logging
import os
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from bondhype.arms import ARMS
from bondhype.decisions import (
    DECISIONS_KEY,
    LLM_ARMS,
    OPENED,
    REJECTED,
    Decision,
    Review,
    encode_decisions,
)
from bondhype.ledger import TRADES_KEY, Ledger, encode_ledger
from bondhype.llm import Verdict
from bondhype.portfolio import PositionOpened, positions_prefix, resolutions_prefix
from bondhype.settlement import STRATEGIES, OverdueReport, Resolution
from bondhype.storage import LocalStorage, S3Storage, Storage

logger = logging.getLogger(__name__)

NOT_CALLED = Verdict(verdict="skipped", risk_flags=(), confidence=None, reason="not called")


class MigrationError(Exception):
    """The per-record files disagree with each other."""


def _parse_ts(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _fetch_all(storage: Storage, keys: list[str], workers: int) -> dict[str, bytes]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(keys, pool.map(storage.get, keys), strict=True))


def build_ledger(storage: Storage, workers: int = 32) -> Ledger:
    ledger = Ledger()
    for arm in ARMS:
        for strategy in STRATEGIES:
            positions = _fetch_all(storage, storage.list(positions_prefix(arm, strategy)), workers)
            for data in positions.values():
                ledger.add_position(PositionOpened.from_json(data))
            resolutions = _fetch_all(
                storage, storage.list(resolutions_prefix(arm, strategy)), workers
            )
            for key, data in resolutions.items():
                resolution = Resolution.from_json(data)
                _check_matches(ledger, resolution.arm, resolution.strategy, resolution, key)
                ledger.add_resolution(resolution)
            # keys sort by market, then date, so the last one per market is its latest analysis
            overdue_keys = storage.list(f"arms/{arm}/{strategy}/overdue/")
            latest: dict[str, str] = {}
            for key in overdue_keys:
                latest[key.split("/")[-2]] = key
            for key, data in _fetch_all(storage, sorted(latest.values()), workers).items():
                report = OverdueReport.from_json(data)
                _check_matches(ledger, report.arm, report.strategy, report, key)
                ledger.set_overdue(report, date.fromisoformat(key.rsplit("/", 1)[1][:10]))
    return ledger


def _check_matches(
    ledger: Ledger, arm: str, strategy: str, record: Resolution | OverdueReport, key: str
) -> None:
    """The table keeps side and config version once, on the position, so they must agree."""
    trade = ledger.trades.get((arm, strategy, record.market_id))
    if trade is None:
        raise MigrationError(f"{key}: no position for it")
    position = trade.position
    if record.config_version != position.config_version:
        raise MigrationError(f"{key}: config version differs from its position")
    if isinstance(record, Resolution) and record.side != position.side:
        raise MigrationError(f"{key}: side differs from its position")


@dataclass
class _Group:
    """Everything the old layout stored about one market in one scan."""

    calls: dict[str, dict] = field(default_factory=dict)  # "buy" / "reject" -> call record
    rejected: dict[str, float] = field(default_factory=dict)  # arm -> price at the rejection


def _prompt_kind(prompt_id: str) -> str:
    for kind in ("buy", "reject"):
        if prompt_id.startswith(f"{kind}_"):
            return kind
    raise MigrationError(f"unknown prompt id {prompt_id}")


def _review(record: dict | None, fallback_prompt: str) -> Review:
    if record is None:
        return Review(prompt_id=fallback_prompt, verdict=NOT_CALLED, cost_usd=None)
    raw = record["verdict"]
    verdict = Verdict(
        verdict=raw["verdict"],
        risk_flags=tuple(raw["risk_flags"]),
        confidence=raw["confidence"],
        reason=raw["reason"],
    )
    return Review(prompt_id=record["prompt_id"], verdict=verdict, cost_usd=record.get("cost_usd"))


@dataclass
class DecisionsBuild:
    decisions: list[Decision]
    ask_mismatches: int


def build_decisions(
    storage: Storage,
    ledger: Ledger,
    buy_prompt: str,
    reject_prompt: str,
    workers: int = 32,
) -> DecisionsBuild:
    groups: dict[tuple[str, str, str], _Group] = defaultdict(_Group)  # (scan, strategy, market)

    call_keys = [k for k in storage.list("llm_calls/") if "/market=" in k]  # old per-call files
    for key, data in _fetch_all(storage, call_keys, workers).items():
        record = json.loads(data)
        snapshot = json.loads(record["user"])
        stamp = key.rsplit("scan=", 1)[1].removesuffix(".json")
        group = groups[(stamp, snapshot["strategy"], record["market_id"])]
        group.calls[_prompt_kind(record["prompt_id"])] = record | {"snapshot": snapshot}

    cooldown_keys = storage.list("state/cooldowns/")
    for key, data in _fetch_all(storage, cooldown_keys, workers).items():
        _, _, arm, strategy, market_id, name = key.split("/")
        stamp = name.removesuffix(".json")
        groups[(stamp, strategy, market_id)].rejected[arm] = json.loads(data)["price"]

    decisions, mismatches = [], 0
    for (stamp, strategy, market_id), group in sorted(groups.items()):
        scan_at = _parse_ts(stamp)
        call = group.calls.get("buy") or group.calls.get("reject")
        snapshot = call["snapshot"] if call else {}
        # A retried scan reused the first attempt's verdict but recorded the cooldown at its own,
        # newer ask; the cooldown price is what kept the market out, so it wins.
        snapshot_ask = snapshot.get("values", {}).get("ask")
        prices = set(group.rejected.values())
        if len(prices) > 1:
            raise MigrationError(f"{stamp} {strategy} {market_id}: arms rejected at {prices}")
        ask = prices.pop() if prices else snapshot_ask
        if ask is None:
            raise MigrationError(f"{stamp} {strategy} {market_id}: no ask")
        if snapshot_ask is not None and round(abs(ask - snapshot_ask), 9) != 0:
            mismatches += 1
        outcomes = dict.fromkeys(group.rejected, REJECTED)
        for arm in LLM_ARMS:
            trade = ledger.trades.get((arm, strategy, market_id))
            if arm not in outcomes and trade and trade.position.opened_at == scan_at:
                outcomes[arm] = OPENED
        decisions.append(
            Decision(
                scan_at=scan_at,
                config_version=call["config_version"] if call else "",
                market_id=market_id,
                strategy=strategy,
                side=snapshot.get("side", ""),
                ask=ask,
                question=snapshot.get("question", ""),
                model=call["model"] if call else "",
                buy=_review(group.calls.get("buy"), buy_prompt),
                reject=_review(group.calls.get("reject"), reject_prompt),
                outcomes={arm: outcomes[arm] for arm in LLM_ARMS if arm in outcomes},
            )
        )
    return DecisionsBuild(decisions, mismatches)


def migrate(
    source: Storage,
    dest: Storage,
    buy_prompt: str,
    reject_prompt: str,
    *,
    workers: int = 32,
    log: Callable[[str], None] = print,
) -> None:
    for key in (TRADES_KEY, DECISIONS_KEY):
        if dest.read_versioned(key).data is not None:
            raise MigrationError(f"{key} already exists; not overwriting it")
    ledger = build_ledger(source, workers)
    statuses: dict[str, int] = defaultdict(int)
    for trade in ledger.trades.values():
        statuses[trade.status] += 1
    log(f"trades: {len(ledger.trades)} ({dict(statuses)})")
    built = build_decisions(source, ledger, buy_prompt, reject_prompt, workers)
    log(f"decisions: {len(built.decisions)}, cooldown/ask mismatches: {built.ask_mismatches}")
    # trades last: once it exists the jobs run, and they need the cooldowns already in place
    dest.write_versioned(DECISIONS_KEY, encode_decisions(built.decisions), None)
    dest.write_versioned(TRADES_KEY, encode_ledger(ledger), None)
    log(f"wrote {DECISIONS_KEY} and {TRADES_KEY}")


def main() -> None:
    from bondhype.config import load_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=os.environ.get("BONDHYPE_BUCKET"))
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--out", type=Path, help="local directory for the tables (default)")
    parser.add_argument("--write", action="store_true", help="write the tables to the bucket")
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()
    if not args.bucket:
        parser.error("pass --bucket or set BONDHYPE_BUCKET")
    if args.write == bool(args.out):
        parser.error("pass exactly one of --out DIR or --write")
    os.environ.setdefault("AWS_REGION", "eu-west-1")
    os.environ.setdefault("AWS_DEFAULT_REGION", os.environ["AWS_REGION"])
    logging.basicConfig(level=logging.INFO)

    config = load_config(args.config)
    source = S3Storage(args.bucket)
    dest = source if args.write else LocalStorage(args.out)
    migrate(source, dest, config.llm.buy_prompt, config.llm.reject_prompt, workers=args.workers)


if __name__ == "__main__":
    main()
