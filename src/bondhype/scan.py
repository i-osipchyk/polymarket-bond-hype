import json
import logging
import math
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from bondhype.arms import ARMS, route
from bondhype.books import BookFetchError, ParseError, parse_book
from bondhype.config import Config
from bondhype.decisions import (
    COOLDOWN,
    HELD,
    LLM_ARMS,
    OPENED,
    REFUSED,
    REJECTED,
    Cooldowns,
    Decision,
    DecisionLog,
    Review,
    load_decisions,
    save_decisions,
)
from bondhype.entry import open_position
from bondhype.filters import Attempt, evaluate_market
from bondhype.ledger import Ledger, load_ledger, save_ledger
from bondhype.llm import LLMSetup, Prompt, ReviewResult, Verdict, review
from bondhype.markets import parse_market
from bondhype.models import Book, Market
from bondhype.storage import Storage

logger = logging.getLogger(__name__)


def _attempt_record(attempt: Attempt) -> dict:
    return {
        "strategy": attempt.strategy,
        "side": attempt.side,
        "passed": attempt.result.passed,
        "reasons": list(attempt.result.reasons),
        "values": {k: (None if math.isnan(v) else v) for k, v in attempt.result.values.items()},
    }


def _load_book(fetch_book: Callable[[str], dict], token_id: str):
    try:
        return parse_book(fetch_book(token_id))
    except (BookFetchError, ParseError):
        return None


def _could_reach_a_band(market, config: Config) -> bool:
    """Generous screen on Gamma prices, since the real ask sits above the quoted price."""
    margin = config.prescreen_price_margin
    bond, hype = config.bond, config.hype
    in_bond_band = any(
        bond.price_min - margin <= price <= bond.price_max + margin
        for price in (market.yes_price, market.no_price)
    )
    in_hype_band = hype.yes_price_min - margin <= market.yes_price <= hype.yes_price_max + margin
    return in_bond_band or in_hype_band


def _snapshot(market: Market, attempt: Attempt, now: datetime) -> dict:
    values = {k: (None if math.isnan(v) else v) for k, v in attempt.result.values.items()}
    return {
        "question": market.question,
        "resolution_rules": market.description,
        "strategy": attempt.strategy,
        "side": attempt.side,
        "end_date": market.end_date.isoformat(),
        "evaluated_at": now.isoformat(),
        "values": values,
    }


@dataclass(frozen=True)
class _Candidate:
    market: Market
    attempt: Attempt
    book: Book
    pending: list[str]  # arms that could still open this market
    skipped: dict[str, str]  # LLM arm -> why it cannot (held, cooldown)
    snapshot: dict


def _arm_states(
    market: Market,
    attempt: Attempt,
    ledger: Ledger,
    cooldowns: Cooldowns,
    config: Config,
) -> tuple[list[str], dict[str, str]]:
    """Arms that could still open this market, and why each other LLM arm cannot."""
    strategy, ask = attempt.strategy, attempt.result.values["ask"]
    pending, skipped = [], {}
    for arm in ARMS:
        if ledger.holds(arm, strategy, market.id):
            skipped[arm] = HELD
        elif arm != "baseline" and cooldowns.active(config, arm, strategy, market.id, ask):
            skipped[arm] = COOLDOWN
        else:
            pending.append(arm)
    return pending, {arm: why for arm, why in skipped.items() if arm in LLM_ARMS}


_SKIPPED = Verdict(
    verdict="skipped", risk_flags=(), confidence=None, reason="buy prompt did not say buy"
)


@dataclass(frozen=True)
class _Reviews:
    buy: Review
    reject: Review  # verdict "skipped" when the buy prompt did not say buy


def _as_review(prompt: Prompt, result: ReviewResult) -> Review:
    return Review(prompt_id=prompt.id, verdict=result.verdict, cost_usd=result.cost_usd)


def _review_all(
    candidates: list[_Candidate],
    config: Config,
    now: datetime,
    llm: LLMSetup,
    records: list[dict],
) -> dict[str, _Reviews]:
    """Buy prompt for every candidate an LLM arm could still trade; reject prompt only for those
    it said buy to. Each phase runs concurrently. A skipped reject prompt counts as no buy.
    Every finished call's record is appended to `records`, even if a later call raises."""
    to_review = [c for c in candidates if any(arm != "baseline" for arm in c.pending)]

    def run(task: tuple[_Candidate, Prompt]) -> ReviewResult:
        candidate, prompt = task
        result = review(
            candidate.snapshot,
            prompt,
            llm.client,
            model=config.llm.model,
            pricing=llm.pricing,
            market_id=candidate.market.id,
            config_version=config.version,
            now=now,
        )
        records.append(result.record)
        return result

    def run_all(tasks: list[tuple[_Candidate, Prompt]]) -> list[ReviewResult]:
        with ThreadPoolExecutor(max_workers=config.llm.max_workers) as pool:
            return list(pool.map(run, tasks))  # map keeps task order

    logger.info("reviewing %d candidates with the buy prompt", len(to_review))
    buy = {
        c.market.id: _as_review(llm.buy, r)
        for c, r in zip(to_review, run_all([(c, llm.buy) for c in to_review]), strict=True)
    }
    bought = [c for c in to_review if buy[c.market.id].verdict.verdict == "buy"]
    logger.info("%d passed the buy prompt, sending them to the reject prompt", len(bought))
    reject = {
        c.market.id: _as_review(llm.reject, r)
        for c, r in zip(bought, run_all([(c, llm.reject) for c in bought]), strict=True)
    }
    skipped = Review(prompt_id=llm.reject.id, verdict=_SKIPPED, cost_usd=None)
    return {
        c.market.id: _Reviews(buy=buy[c.market.id], reject=reject.get(c.market.id, skipped))
        for c in to_review
    }


def llm_calls_key(now: datetime, run_id: str) -> str:
    """One audit file per scan attempt, so a retried scan never overwrites the spend it repeats."""
    return f"llm_calls/date={now:%Y-%m-%d}/scan={now:%Y-%m-%dT%H:%M:%SZ}/run={run_id}.jsonl"


def _write_llm_calls(storage: Storage, records: list[dict], now: datetime) -> None:
    if records:
        lines = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
        storage.put(llm_calls_key(now, uuid.uuid4().hex[:12]), lines.encode())


def _enter_arms(
    candidate: _Candidate,
    reviews: _Reviews | None,
    ledger: Ledger,
    log: DecisionLog,
    config: Config,
    now: datetime,
) -> None:
    market, attempt = candidate.market, candidate.attempt
    strategy, ask = attempt.strategy, attempt.result.values["ask"]
    trading = {"baseline"}
    outcomes = dict(candidate.skipped)
    if reviews is not None:
        trading = route(reject=reviews.reject.verdict, buy=reviews.buy.verdict)
        for arm in candidate.pending:
            if arm not in trading:
                outcomes[arm] = REJECTED
    for arm in candidate.pending:
        if arm in trading:
            entry = open_position(
                config,
                ledger,
                arm=arm,
                strategy=strategy,
                market=market,
                side=attempt.side,
                book=candidate.book,
                now=now,
            )
            if entry.rejection is None:
                logger.info("opened %s/%s %s on %s", arm, strategy, attempt.side, market.id)
                outcomes[arm] = OPENED
                continue
            logger.warning(
                "entry refused %s/%s on %s: %s", arm, strategy, market.id, entry.rejection
            )
            outcomes[arm] = REFUSED + entry.rejection  # a refused entry cools down like a reject
    if reviews is not None:
        log.add(
            Decision(
                scan_at=now,
                config_version=config.version,
                market_id=market.id,
                strategy=strategy,
                side=attempt.side,
                ask=ask,
                question=market.question,
                model=config.llm.model,
                buy=reviews.buy,
                reject=reviews.reject,
                outcomes={arm: o for arm, o in outcomes.items() if arm in LLM_ARMS},
            )
        )


def _skipped_line(market_id: str, config: Config, **fields) -> str:
    return json.dumps(
        {"market_id": market_id, "config_version": config.version, "selected": None, "attempts": []}
        | fields,
        allow_nan=False,
    )


def scan(
    raw_markets: Iterable[dict],
    fetch_book: Callable[[str], dict],
    storage: Storage,
    config: Config,
    now: datetime,
    llm: LLMSetup | None = None,
) -> None:
    """Evaluate every market, review candidates concurrently, then open positions in order.

    Reads the trades and decisions tables once and writes each once, plus one LLM audit file and
    one candidates file."""
    ledger = load_ledger(storage)
    log = load_decisions(storage)
    log.begin_scan(now)
    cooldowns = Cooldowns(log.decisions)
    lines: list[str] = []
    candidates: list[_Candidate] = []
    seen = prescreened = 0
    for raw in raw_markets:
        seen += 1
        if seen % 100 == 0:
            logger.info("evaluated %d markets, %d candidates so far", seen, len(candidates))
        try:
            market = parse_market(raw)
        except ParseError as exc:
            logger.warning("market %s unparseable: %s", raw.get("id"), exc)
            lines.append(_skipped_line(str(raw.get("id")), config, parse_error=str(exc)))
            continue
        if not _could_reach_a_band(market, config):
            prescreened += 1
            lines.append(_skipped_line(market.id, config, prescreened="no_price_in_any_band"))
            continue
        books = {
            side: book
            for side, token in (("YES", market.yes_token), ("NO", market.no_token))
            if (book := _load_book(fetch_book, token)) is not None
        }
        evaluation = evaluate_market(market, books, config, now)
        selected = evaluation.selected
        if selected is not None:
            logger.info(
                "candidate %s %s %s ask=%.3f: %s",
                market.id,
                selected.strategy,
                selected.side,
                selected.result.values["ask"],
                market.question[:60],
            )
            pending, skipped = _arm_states(market, selected, ledger, cooldowns, config)
            candidates.append(
                _Candidate(
                    market=market,
                    attempt=selected,
                    book=books[selected.side],
                    pending=pending,
                    skipped=skipped,
                    snapshot=_snapshot(market, selected, now),
                )
            )
        lines.append(
            json.dumps(
                {
                    "market_id": market.id,
                    "config_version": config.version,
                    "selected": (
                        {"strategy": selected.strategy, "side": selected.side} if selected else None
                    ),
                    "attempts": [_attempt_record(a) for a in evaluation.attempts],
                },
                allow_nan=False,
            )
        )
    logger.info(
        "fetched and evaluated %d markets: %d prescreened out, %d candidates",
        seen,
        prescreened,
        len(candidates),
    )

    reviews: dict[str, _Reviews] = {}
    if llm is not None:
        records: list[dict] = []
        try:
            reviews = _review_all(candidates, config, now, llm, records)
        finally:
            _write_llm_calls(storage, records, now)  # the spend is kept even if a call raised
    for candidate in candidates:
        _enter_arms(candidate, reviews.get(candidate.market.id), ledger, log, config, now)
    save_ledger(storage, ledger)
    save_decisions(storage, log)

    key = f"candidates/date={now:%Y-%m-%d}/scan={now:%Y-%m-%dT%H:%M:%SZ}.jsonl"
    storage.put(key, ("\n".join(lines) + "\n").encode())
    logger.info("scan done, wrote %s", key)
