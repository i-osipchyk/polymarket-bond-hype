from dataclasses import dataclass
from datetime import datetime

from bondhype.config import BondFilters, Config, HypeFilters
from bondhype.models import Book, Market

SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reasons: tuple[str, ...]
    values: dict[str, float]


def _book_values(market: Market, book: Book, now: datetime) -> dict[str, float] | None:
    asks = sorted(book.asks, key=lambda level: level.price)
    bids = sorted(book.bids, key=lambda level: level.price, reverse=True)
    if not asks or not bids:
        return None
    ask, bid = asks[0].price, bids[0].price
    return {
        "volume_usd": market.volume_usd,
        "ask": ask,
        "spread": ask - bid,
        "days_to_resolution": (market.end_date - now).total_seconds() / SECONDS_PER_DAY,
        "liquidity_usd": sum(level.price * level.size for level in asks[:2]),
    }


def _shared_reasons(
    values: dict[str, float], rules: BondFilters | HypeFilters, order_size_usd: float
) -> list[str]:
    reasons = []
    if (
        not rules.days_to_resolution_min
        <= values["days_to_resolution"]
        <= (rules.days_to_resolution_max)
    ):
        reasons.append("days_out_of_range")
    if round(values["spread"], 6) > rules.max_spread:
        reasons.append("spread_too_wide")
    if values["liquidity_usd"] < rules.liquidity_multiple * order_size_usd:
        reasons.append("insufficient_liquidity")
    if values["volume_usd"] < rules.min_total_volume_usd:
        reasons.append("volume_below_floor")
    return reasons


def evaluate_bond(
    market: Market, side: str, book: Book, config: Config, now: datetime
) -> FilterResult:
    values = _book_values(market, book, now)
    if values is None:
        return FilterResult(passed=False, reasons=("missing_book_data",), values={})
    days, ask = values["days_to_resolution"], values["ask"]
    values["annualised_yield"] = (1 - ask) / ask * 365 / days if days > 0 else float("nan")
    reasons = []
    if not config.bond.price_min <= ask <= config.bond.price_max:
        reasons.append("price_out_of_band")
    reasons += _shared_reasons(values, config.bond, config.order_size_usd)
    if values["annualised_yield"] < config.bond.min_annualised_yield:
        reasons.append("yield_below_threshold")
    return FilterResult(passed=not reasons, reasons=tuple(reasons), values=values)


def evaluate_hype(market: Market, book: Book, config: Config, now: datetime) -> FilterResult:
    values = _book_values(market, book, now)
    if values is None:
        return FilterResult(passed=False, reasons=("missing_book_data",), values={})
    values["yes_price"] = market.yes_price
    reasons = []
    if not config.hype.yes_price_min <= market.yes_price <= config.hype.yes_price_max:
        reasons.append("yes_price_out_of_band")
    reasons += _shared_reasons(values, config.hype, config.order_size_usd)
    return FilterResult(passed=not reasons, reasons=tuple(reasons), values=values)


@dataclass(frozen=True)
class Attempt:
    strategy: str
    side: str
    result: FilterResult


@dataclass(frozen=True)
class Evaluation:
    market_id: str
    selected: Attempt | None
    attempts: tuple[Attempt, ...]


def evaluate_market(
    market: Market, books: dict[str, Book], config: Config, now: datetime
) -> Evaluation:
    empty = Book(bids=(), asks=())
    attempts = [
        Attempt("bond", side, evaluate_bond(market, side, books.get(side, empty), config, now))
        for side in ("YES", "NO")
    ]
    attempts.append(
        Attempt("hype", "NO", evaluate_hype(market, books.get("NO", empty), config, now))
    )
    selected = next((attempt for attempt in attempts if attempt.result.passed), None)
    return Evaluation(market_id=market.id, selected=selected, attempts=tuple(attempts))
