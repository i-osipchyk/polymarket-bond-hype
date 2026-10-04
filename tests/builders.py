from datetime import UTC, datetime, timedelta

from bondhype.models import Book, FeeSchedule, Level, Market

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def make_market(**overrides) -> Market:
    fields = dict(
        id="m1",
        question="Will X happen?",
        end_date=NOW + timedelta(days=6),
        volume_usd=10_000.0,
        yes_price=0.07,
        no_price=0.93,
        event_id="e1",
        fee_schedule=FeeSchedule(rate=0.05, exponent=1, enabled=True),
    )
    fields.update(overrides)
    return Market(**fields)


def make_book(bid=0.92, ask=0.93, ask_size=100.0, next_ask_size=100.0) -> Book:
    return Book(
        bids=(Level(bid, 500.0),),
        asks=(Level(ask, ask_size), Level(round(ask + 0.01, 4), next_ask_size)),
    )
