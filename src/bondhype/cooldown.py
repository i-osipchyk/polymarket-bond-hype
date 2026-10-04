import json
from datetime import datetime, timedelta

from bondhype.config import Config
from bondhype.storage import Storage


def _prefix(arm: str, strategy: str, market_id: str) -> str:
    return f"state/cooldowns/{arm}/{strategy}/{market_id}/"


def record_rejection(
    storage: Storage, arm: str, strategy: str, market_id: str, price: float, now: datetime
) -> None:
    record = {"rejected_at": now.isoformat(), "price": price}
    key = f"{_prefix(arm, strategy, market_id)}{now:%Y-%m-%dT%H:%M:%SZ}.json"
    storage.put(key, json.dumps(record, sort_keys=True).encode())


def cooldown_active(
    storage: Storage,
    config: Config,
    arm: str,
    strategy: str,
    market_id: str,
    price: float,
    now: datetime,
) -> bool:
    keys = storage.list(_prefix(arm, strategy, market_id))
    if not keys:
        return False
    last = json.loads(storage.get(keys[-1]))
    rejected_at = datetime.fromisoformat(last["rejected_at"])
    if now - rejected_at >= timedelta(hours=config.portfolio.cooldown_hours):
        return False
    return round(abs(price - last["price"]), 6) <= config.portfolio.cooldown_price_move
