import json
import math
from collections.abc import Callable, Iterable
from datetime import datetime

from bondhype.arms import ARMS, route
from bondhype.books import BookFetchError, ParseError, parse_book
from bondhype.config import Config
from bondhype.cooldown import cooldown_active, record_rejection
from bondhype.entry import open_position
from bondhype.filters import Attempt, evaluate_market
from bondhype.llm import LLMSetup, review
from bondhype.markets import parse_market
from bondhype.models import Book, Market
from bondhype.portfolio import position_key
from bondhype.storage import Storage


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


def _enter_arms(
    market: Market,
    attempt: Attempt,
    book: Book,
    storage: Storage,
    config: Config,
    now: datetime,
    llm: LLMSetup | None,
) -> None:
    strategy, ask = attempt.strategy, attempt.result.values["ask"]
    pending = [
        arm
        for arm in ARMS
        if not storage.exists(position_key(arm, strategy, market.id))
        and (
            arm == "baseline"
            or not cooldown_active(storage, config, arm, strategy, market.id, ask, now)
        )
    ]
    trading = {"baseline"}
    if llm is not None and any(arm != "baseline" for arm in pending):
        snapshot = _snapshot(market, attempt, now)
        verdicts = [
            review(
                snapshot,
                prompt,
                llm.client,
                model=config.llm.model,
                storage=storage,
                market_id=market.id,
                config_version=config.version,
                now=now,
            )
            for prompt in (llm.reject, llm.buy)
        ]
        trading = route(reject=verdicts[0], buy=verdicts[1])
        for arm in pending:
            if arm not in trading:
                record_rejection(storage, arm, strategy, market.id, ask, now)
    for arm in pending:
        if arm in trading:
            open_position(
                storage,
                config,
                arm=arm,
                strategy=strategy,
                market=market,
                side=attempt.side,
                book=book,
                now=now,
            )


def scan(
    raw_markets: Iterable[dict],
    fetch_book: Callable[[str], dict],
    storage: Storage,
    config: Config,
    now: datetime,
    llm: LLMSetup | None = None,
) -> None:
    lines = []
    for raw in raw_markets:
        try:
            market = parse_market(raw)
        except ParseError as exc:
            lines.append(
                json.dumps(
                    {
                        "market_id": str(raw.get("id")),
                        "config_version": config.version,
                        "selected": None,
                        "parse_error": str(exc),
                        "attempts": [],
                    },
                    allow_nan=False,
                )
            )
            continue
        if not _could_reach_a_band(market, config):
            lines.append(
                json.dumps(
                    {
                        "market_id": market.id,
                        "config_version": config.version,
                        "selected": None,
                        "prescreened": "no_price_in_any_band",
                        "attempts": [],
                    },
                    allow_nan=False,
                )
            )
            continue
        books = {
            side: book
            for side, token in (("YES", market.yes_token), ("NO", market.no_token))
            if (book := _load_book(fetch_book, token)) is not None
        }
        evaluation = evaluate_market(market, books, config, now)
        selected = evaluation.selected
        if selected is not None:
            _enter_arms(market, selected, books[selected.side], storage, config, now, llm)
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
    key = f"candidates/date={now:%Y-%m-%d}/scan={now:%Y-%m-%dT%H:%M:%SZ}.jsonl"
    storage.put(key, ("\n".join(lines) + "\n").encode())
