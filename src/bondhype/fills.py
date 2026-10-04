from dataclasses import dataclass

from bondhype.models import Book, FeeSchedule


class UnsupportedFeeSchedule(Exception):
    """Only the documented exponent-1 formula is implemented; anything else fails closed."""


@dataclass(frozen=True)
class Fill:
    shares: float
    filled_usd: float
    avg_price: float
    best_ask: float
    slippage_per_share: float
    gross_edge_per_share: float
    fee_usd: float
    partial: bool


def simulate_fill(book: Book, order_usd: float, fees: FeeSchedule) -> Fill | None:
    asks = sorted((lvl for lvl in book.asks if lvl.size > 0), key=lambda level: level.price)
    if not asks:
        return None
    if fees.enabled and fees.exponent != 1:
        raise UnsupportedFeeSchedule(f"fee exponent {fees.exponent}")
    remaining = order_usd
    shares = 0.0
    fee = 0.0
    for level in asks:
        if remaining <= 0:
            break
        take = min(level.size, remaining / level.price)
        shares += take
        remaining -= take * level.price
        fee += take * (fees.rate if fees.enabled else 0.0) * level.price * (1 - level.price)
    best_ask = asks[0].price
    filled_usd = order_usd - remaining
    avg_price = filled_usd / shares
    return Fill(
        shares=shares,
        filled_usd=filled_usd,
        avg_price=avg_price,
        best_ask=best_ask,
        slippage_per_share=avg_price - best_ask,
        gross_edge_per_share=1 - best_ask,
        fee_usd=round(fee, 5),
        partial=remaining > 1e-9,
    )
