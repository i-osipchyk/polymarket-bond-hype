import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from bondhype.books import BookFetchError
from bondhype.portfolio import PositionOpened
from bondhype.settlement import resolution_key, resolve, unsettled_positions
from bondhype.storage import ImmutableKey, Storage


def pricepath_key(arm: str, strategy: str, market_id: str, now: datetime) -> str:
    return f"arms/{arm}/{strategy}/pricepath/{market_id}/{now.strftime('%Y-%m-%dT%H')}.json"


def _snapshot(
    position: PositionOpened,
    raw_market: dict,
    fetch_book: Callable[[str], dict],
    now: datetime,
) -> dict:
    yes_price, no_price = (float(p) for p in json.loads(raw_market["outcomePrices"]))
    yes_token, no_token = json.loads(raw_market["clobTokenIds"])
    try:
        book = fetch_book(no_token if position.side == "NO" else yes_token)
    except BookFetchError:
        book = None
    return {
        "observed_at": now.isoformat(),
        "yes_price": yes_price,
        "no_price": no_price,
        "book": book,
    }


@dataclass
class TrackSummary:
    settled: int = 0
    snapshotted: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def _track_position(
    storage: Storage,
    position: PositionOpened,
    fetch_market: Callable[[str], dict],
    fetch_book: Callable[[str], dict],
    now: datetime,
    summary: TrackSummary,
) -> None:
    arm, strategy = position.arm, position.strategy
    raw_market = fetch_market(position.market_id)
    resolution = resolve(position, raw_market)
    if resolution is not None:
        storage.put(resolution_key(arm, strategy, position.market_id), resolution.to_json())
        summary.settled += 1
        return
    snapshot = _snapshot(position, raw_market, fetch_book, now)
    try:
        storage.put(
            pricepath_key(arm, strategy, position.market_id, now),
            json.dumps(snapshot, sort_keys=True).encode(),
        )
    except ImmutableKey:
        return  # an earlier run this hour already recorded the point
    summary.snapshotted += 1


def track(
    storage: Storage,
    fetch_market: Callable[[str], dict],
    fetch_book: Callable[[str], dict],
    now: datetime,
) -> TrackSummary:
    summary = TrackSummary()
    for position in unsettled_positions(storage):
        try:
            _track_position(storage, position, fetch_market, fetch_book, now, summary)
        except (OSError, ValueError, KeyError) as exc:
            summary.errors.append((position.market_id, repr(exc)))
    return summary
