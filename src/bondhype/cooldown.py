import json
from datetime import datetime

from bondhype.config import Config
from bondhype.storage import Storage


def _arm_prefix(arm: str, strategy: str) -> str:
    return f"state/cooldowns/{arm}/{strategy}/"


def _prefix(arm: str, strategy: str, market_id: str) -> str:
    return f"{_arm_prefix(arm, strategy)}{market_id}/"


def record_rejection(
    storage: Storage, arm: str, strategy: str, market_id: str, price: float, now: datetime
) -> None:
    record = {"rejected_at": now.isoformat(), "price": price}
    key = f"{_prefix(arm, strategy, market_id)}{now:%Y-%m-%dT%H:%M:%SZ}.json"
    storage.put(key, json.dumps(record, sort_keys=True).encode())


class Cooldowns:
    """Latest rejection per market, one listing per arm and strategy for a whole scan."""

    def __init__(self, storage: Storage):
        self._storage = storage
        self._latest: dict[tuple[str, str], dict[str, str]] = {}

    def _latest_keys(self, arm: str, strategy: str) -> dict[str, str]:
        if (arm, strategy) not in self._latest:
            prefix = _arm_prefix(arm, strategy)
            latest: dict[str, str] = {}
            for key in self._storage.list(prefix):  # sorted, so the last key per market wins
                latest[key.removeprefix(prefix).split("/", 1)[0]] = key
            self._latest[(arm, strategy)] = latest
        return self._latest[(arm, strategy)]

    def active(self, config: Config, arm: str, strategy: str, market_id: str, price: float) -> bool:
        """A rejected market stays out until its price is at least `cooldown_price_move` away from
        the price at the last rejection, either way. Time alone never ends a cooldown."""
        key = self._latest_keys(arm, strategy).get(market_id)
        if key is None:
            return False
        last = json.loads(self._storage.get(key))
        return round(abs(price - last["price"]), 6) < config.portfolio.cooldown_price_move
