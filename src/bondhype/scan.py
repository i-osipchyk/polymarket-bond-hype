import json
import math
from collections.abc import Callable, Iterable
from datetime import datetime

from bondhype.books import BookFetchError, ParseError, parse_book
from bondhype.config import Config
from bondhype.filters import Attempt, evaluate_market
from bondhype.markets import parse_market
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


def scan(
    raw_markets: Iterable[dict],
    fetch_book: Callable[[str], dict],
    storage: Storage,
    config: Config,
    now: datetime,
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
