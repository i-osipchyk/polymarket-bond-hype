import json
from datetime import datetime

from bondhype.books import ParseError
from bondhype.models import Market


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
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ParseError(f"unexpected Gamma market shape: {exc!r}") from exc
