from collections.abc import Callable
from datetime import datetime

from bondhype.settlement import assess_overdue, unsettled_positions
from bondhype.storage import ImmutableKey, Storage


def overdue_key(arm: str, strategy: str, market_id: str, now: datetime) -> str:
    return f"arms/{arm}/{strategy}/overdue/{market_id}/{now.strftime('%Y-%m-%d')}.json"


def run_overdue(
    storage: Storage, fetch_market: Callable[[str], dict], now: datetime
) -> list[tuple[str, str]]:
    """Write today's analysis for each overdue position. Returns (market_id, error) pairs."""
    errors: list[tuple[str, str]] = []
    for position in unsettled_positions(storage):
        try:
            report = assess_overdue(position, fetch_market(position.market_id), now)
            if report is None:
                continue
            key = overdue_key(position.arm, position.strategy, position.market_id, now)
            try:
                storage.put(key, report.to_json())
            except ImmutableKey:
                pass  # an earlier run today already recorded it
        except (OSError, ValueError, KeyError) as exc:
            errors.append((position.market_id, repr(exc)))
    return errors
