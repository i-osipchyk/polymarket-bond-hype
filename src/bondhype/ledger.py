"""Every position and resolution, loaded once per run.

A full read costs one request per stored record, and the ledger only grows, so the scanner leaves
a checkpoint: one gzipped object holding every record it knew. The next run reads the newest
checkpoint and fetches only keys written after it. Records are immutable, so any checkpoint is a
correct subset of the truth; the per-record files stay the source of truth.
"""

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bondhype.arms import ARMS
from bondhype.config import Config
from bondhype.portfolio import Portfolio, PositionOpened, positions_prefix, resolutions_prefix
from bondhype.settlement import STRATEGIES, Resolution
from bondhype.storage import ImmutableKey, Storage

CHECKPOINT_PREFIX = "state/ledger/"


def _checkpoint_dir(day: datetime) -> str:
    return f"{CHECKPOINT_PREFIX}date={day:%Y-%m-%d}/"


def checkpoint_key(now: datetime) -> str:
    return f"{_checkpoint_dir(now)}scan={now:%Y-%m-%dT%H:%M:%SZ}.json.gz"


@dataclass
class Ledger:
    """Records by storage key, so a checkpoint and a listing can be diffed by key."""

    position_records: dict[str, PositionOpened] = field(default_factory=dict)
    resolution_records: dict[str, Resolution] = field(default_factory=dict)

    def positions(self, arm: str, strategy: str) -> list[PositionOpened]:
        prefix = positions_prefix(arm, strategy)
        return [p for k, p in sorted(self.position_records.items()) if k.startswith(prefix)]

    def resolutions(self, arm: str, strategy: str) -> list[Resolution]:
        prefix = resolutions_prefix(arm, strategy)
        return [r for k, r in sorted(self.resolution_records.items()) if k.startswith(prefix)]

    def holds(self, arm: str, strategy: str, market_id: str) -> bool:
        return f"{positions_prefix(arm, strategy)}{market_id}.json" in self.position_records

    def is_settled(self, position: PositionOpened) -> bool:
        key = f"{resolutions_prefix(position.arm, position.strategy)}{position.market_id}.json"
        return key in self.resolution_records

    def unsettled(self) -> list[PositionOpened]:
        """Open positions in arm, then strategy, then market order."""
        return [
            p
            for arm in ARMS
            for strategy in STRATEGIES
            for p in self.positions(arm, strategy)
            if not self.is_settled(p)
        ]

    def portfolio(self, config: Config, arm: str, strategy: str) -> Portfolio:
        return Portfolio.from_events(
            self.positions(arm, strategy),
            config.portfolio.starting_balance_usd,
            self.resolutions(arm, strategy),
        )

    def add_position(self, key: str, position: PositionOpened) -> None:
        self.position_records[key] = position

    def add_resolution(self, key: str, resolution: Resolution) -> None:
        self.resolution_records[key] = resolution


def _latest_checkpoint(storage: Storage, now: datetime) -> Ledger:
    for day in (now, now - timedelta(days=1)):
        keys = [k for k in storage.list(_checkpoint_dir(day)) if k <= checkpoint_key(now)]
        if keys:
            record = json.loads(gzip.decompress(storage.get(keys[-1])))
            return Ledger(
                {k: PositionOpened.from_json(v.encode()) for k, v in record["positions"].items()},
                {k: Resolution.from_json(v.encode()) for k, v in record["resolutions"].items()},
            )
    return Ledger()


def load_ledger(storage: Storage, now: datetime, use_checkpoint: bool = True) -> Ledger:
    """Newest checkpoint from today or yesterday, plus every record stored since."""
    ledger = _latest_checkpoint(storage, now) if use_checkpoint else Ledger()
    for arm in ARMS:
        for strategy in STRATEGIES:
            for key in storage.list(positions_prefix(arm, strategy)):
                if key not in ledger.position_records:
                    ledger.add_position(key, PositionOpened.from_json(storage.get(key)))
            for key in storage.list(resolutions_prefix(arm, strategy)):
                if key not in ledger.resolution_records:
                    ledger.add_resolution(key, Resolution.from_json(storage.get(key)))
    return ledger


def save_checkpoint(storage: Storage, ledger: Ledger, now: datetime) -> None:
    record = {
        "positions": {k: p.to_json().decode() for k, p in sorted(ledger.position_records.items())},
        "resolutions": {
            k: r.to_json().decode() for k, r in sorted(ledger.resolution_records.items())
        },
    }
    data = gzip.compress(json.dumps(record, sort_keys=True).encode(), mtime=0)
    try:
        storage.put(checkpoint_key(now), data)
    except ImmutableKey:
        pass  # a retried run: the first checkpoint is a valid subset and stays
