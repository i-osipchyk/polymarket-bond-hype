import json
from datetime import datetime

from bondhype.books import ParseError
from bondhype.models import FeeSchedule, Market


def _parse_fee_schedule(raw: dict) -> FeeSchedule | None:
    schedule = raw.get("feeSchedule")
    if not schedule:
        if raw.get("feesEnabled") is False:
            return FeeSchedule(rate=0.0, exponent=1, enabled=False)
        return None
    return FeeSchedule(
        rate=float(schedule["rate"]),
        exponent=float(schedule["exponent"]),
        enabled=bool(raw.get("feesEnabled")),
    )


def parse_market(raw: dict) -> Market:
    """Gamma encodes outcomes, prices and token ids as JSON strings inside the JSON."""
    try:
        outcomes = json.loads(raw["outcomes"])
        prices = [float(p) for p in json.loads(raw["outcomePrices"])]
        tokens = json.loads(raw["clobTokenIds"])
        if not len(outcomes) == len(prices) == len(tokens) == 2:
            raise ValueError("expected exactly two outcomes")
        return Market(
            id=str(raw["id"]),
            question=raw["question"],
            end_date=datetime.fromisoformat(raw["endDate"]),
            volume_usd=float(raw["volumeNum"]),
            yes_price=prices[0],
            no_price=prices[1],
            event_id=str(raw["events"][0]["id"]),
            yes_token=tokens[0],
            no_token=tokens[1],
            outcomes=(outcomes[0], outcomes[1]),
            fee_schedule=_parse_fee_schedule(raw),
            description=raw.get("description") or "",
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ParseError(f"unexpected Gamma market shape: {exc!r}") from exc
