from collections.abc import Callable
from datetime import datetime

from bondhype.ledger import load_ledger, save_ledger
from bondhype.settlement import assess_overdue
from bondhype.storage import Storage


def run_overdue(
    storage: Storage, fetch_market: Callable[[str], dict], now: datetime
) -> list[tuple[str, str]]:
    """Record today's analysis on each overdue trade, then write the trades table once.
    Returns (market_id, error) pairs."""
    errors: list[tuple[str, str]] = []
    ledger = load_ledger(storage)
    for position in ledger.unsettled():
        try:
            report = assess_overdue(position, fetch_market(position.market_id), now)
        except (OSError, ValueError, KeyError) as exc:
            errors.append((position.market_id, repr(exc)))
            continue
        if report is not None:
            ledger.set_overdue(report, now.date())
    save_ledger(storage, ledger)
    return errors
