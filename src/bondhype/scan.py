import json
import logging
import math
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from bondhype.arms import ARMS, route
from bondhype.books import BookFetchError, ParseError, parse_book
from bondhype.config import Config
from bondhype.cooldown import cooldown_active, record_rejection
from bondhype.entry import open_position
from bondhype.filters import Attempt, evaluate_market
from bondhype.llm import LLMSetup, Prompt, Verdict, review
from bondhype.markets import parse_market
from bondhype.models import Book, Market
from bondhype.portfolio import position_key
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
    snapshot: dict


def _pending_arms(
    market: Market, attempt: Attempt, storage: Storage, config: Config, now: datetime
) -> list[str]:
    strategy, ask = attempt.strategy, attempt.result.values["ask"]
    return [
        arm
        for arm in ARMS
        if not storage.exists(position_key(arm, strategy, market.id))
        and (
            arm == "baseline" or not cooldown_active(storage, config, arm, strategy, market.id, ask)
        )
    ]


_SKIPPED = Verdict(
    verdict="skipped", risk_flags=(), confidence=None, reason="buy prompt did not say buy"
)


def _review_all(
    candidates: list[_Candidate], storage: Storage, config: Config, now: datetime, llm: LLMSetup
) -> dict[str, tuple[Verdict, Verdict]]:
    """Buy prompt for every candidate an LLM arm could still trade; reject prompt only for those
    it said buy to. Each phase runs concurrently. A skipped reject prompt counts as no buy."""
    to_review = [c for c in candidates if any(arm != "baseline" for arm in c.pending)]

    def run(task: tuple[_Candidate, Prompt]) -> Verdict:
        candidate, prompt = task
        return review(
            candidate.snapshot,
            prompt,
            llm.client,
            model=config.llm.model,
            pricing=llm.pricing,
            storage=storage,
            market_id=candidate.market.id,
            config_version=config.version,
            now=now,
        )

    def run_all(tasks: list[tuple[_Candidate, Prompt]]) -> list[Verdict]:
        with ThreadPoolExecutor(max_workers=config.llm.max_workers) as pool:
            return list(pool.map(run, tasks))  # map keeps task order

    logger.info("reviewing %d candidates with the buy prompt", len(to_review))
    buy = {
        c.market.id: v
        for c, v in zip(to_review, run_all([(c, llm.buy) for c in to_review]), strict=True)
    }
    bought = [c for c in to_review if buy[c.market.id].verdict == "buy"]
    logger.info("%d passed the buy prompt, sending them to the reject prompt", len(bought))
    reject = {
        c.market.id: v
        for c, v in zip(bought, run_all([(c, llm.reject) for c in bought]), strict=True)
    }
    return {c.market.id: (reject.get(c.market.id, _SKIPPED), buy[c.market.id]) for c in to_review}


def _enter_arms(
    candidate: _Candidate,
    verdicts: tuple[Verdict, Verdict] | None,
    storage: Storage,
    config: Config,
    now: datetime,
) -> None:
    market, attempt = candidate.market, candidate.attempt
    strategy, ask = attempt.strategy, attempt.result.values["ask"]
    trading = {"baseline"}
    if verdicts is not None:
        trading = route(reject=verdicts[0], buy=verdicts[1])
        for arm in candidate.pending:
            if arm not in trading:
                record_rejection(storage, arm, strategy, market.id, ask, now)
    for arm in candidate.pending:
        if arm in trading:
            entry = open_position(
                storage,
                config,
                arm=arm,
                strategy=strategy,
                market=market,
                side=attempt.side,
                book=candidate.book,
                now=now,
            )
            if entry.rejection is None:
                logger.info("opened %s/%s %s on %s", arm, strategy, attempt.side, market.id)
                continue
            logger.warning(
                "entry refused %s/%s on %s: %s", arm, strategy, market.id, entry.rejection
            )
            if arm != "baseline":  # a refused entry is cached like a rejection
                record_rejection(storage, arm, strategy, market.id, ask, now)


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
    """Evaluate every market, review candidates concurrently, then open positions in order."""
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
            candidates.append(
                _Candidate(
                    market=market,
                    attempt=selected,
                    book=books[selected.side],
                    pending=_pending_arms(market, selected, storage, config, now),
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

    verdicts = _review_all(candidates, storage, config, now, llm) if llm is not None else {}
    for candidate in candidates:
        _enter_arms(candidate, verdicts.get(candidate.market.id), storage, config, now)

    key = f"candidates/date={now:%Y-%m-%d}/scan={now:%Y-%m-%dT%H:%M:%SZ}.jsonl"
    storage.put(key, ("\n".join(lines) + "\n").encode())
    logger.info("scan done, wrote %s", key)
