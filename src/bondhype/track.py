import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from bondhype.books import BookFetchError
from bondhype.ledger import load_ledger
from bondhype.portfolio import PositionOpened
from bondhype.settlement import resolution_key, resolve
from bondhype.storage import ImmutableKey, Storage


def pricepath_key(now: datetime) -> str:
    """One file per tracker run, at most one run's file per hour."""
    return f"pricepath/date={now:%Y-%m-%d}/run={now:%Y-%m-%dT%H}.jsonl"


def _price_point(
    market_id: str,
    holders: list[PositionOpened],
    raw_market: dict,
    fetch_book: Callable[[str], dict],
    now: datetime,
) -> dict:
    yes_price, no_price = (float(p) for p in json.loads(raw_market["outcomePrices"]))
    tokens = dict(zip(("YES", "NO"), json.loads(raw_market["clobTokenIds"]), strict=True))
    books = {}
    for side in sorted({p.side for p in holders}):
        try:
            books[side] = fetch_book(tokens[side])
        except BookFetchError:
            books[side] = None
    return {
        "market_id": market_id,
        "holders": [{"arm": p.arm, "strategy": p.strategy, "side": p.side} for p in holders],
        "observed_at": now.isoformat(),
        "yes_price": yes_price,
        "no_price": no_price,
        "books": books,
    }


@dataclass
class TrackSummary:
    settled: int = 0
    snapshotted: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def _track_market(
    storage: Storage,
    market_id: str,
    positions: list[PositionOpened],
    fetch_market: Callable[[str], dict],
    fetch_book: Callable[[str], dict],
    now: datetime,
    summary: TrackSummary,
) -> dict | None:
    """Settle what has resolved; return a price point for the rest, if any are still open."""
    raw_market = fetch_market(market_id)
    still_open = []
    for position in positions:
        resolution = resolve(position, raw_market)
        if resolution is None:
            still_open.append(position)
            continue
        key = resolution_key(position.arm, position.strategy, market_id)
        storage.put(key, resolution.to_json())
        summary.settled += 1
    if not still_open:
        return None
    return _price_point(market_id, still_open, raw_market, fetch_book, now)


def track(
    storage: Storage,
    fetch_market: Callable[[str], dict],
    fetch_book: Callable[[str], dict],
    now: datetime,
) -> TrackSummary:
    """Each market is fetched once however many arms hold it; price points go to one file."""
    summary = TrackSummary()
    by_market: dict[str, list[PositionOpened]] = {}
    for position in load_ledger(storage, now).unsettled():
        by_market.setdefault(position.market_id, []).append(position)
    points = []
    for market_id, positions in by_market.items():
        try:
            point = _track_market(
                storage, market_id, positions, fetch_market, fetch_book, now, summary
            )
        except (OSError, ValueError, KeyError) as exc:
            summary.errors.append((market_id, repr(exc)))
            continue
        if point is not None:
            points.append(point)
    if points:
        data = "".join(json.dumps(p, sort_keys=True) + "\n" for p in points).encode()
        try:
            storage.put(pricepath_key(now), data)
        except ImmutableKey:
            return summary  # an earlier run this hour already recorded the points
        summary.snapshotted = sum(len(p["holders"]) for p in points)
    return summary
