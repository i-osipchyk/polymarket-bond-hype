"""Run one scan against live data, writing candidates to a local directory."""

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bondhype import clients
from bondhype.config import load_config
from bondhype.scan import scan
from bondhype.storage import LocalStorage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()

    config = load_config(args.config)
    now = datetime.now(UTC).replace(microsecond=0)
    window_days = max(config.bond.days_to_resolution_max, config.hype.days_to_resolution_max)
    markets = clients.list_markets(
        end_date_min=now
        + timedelta(
            days=min(config.bond.days_to_resolution_min, config.hype.days_to_resolution_min)
        ),
        end_date_max=now + timedelta(days=window_days),
        volume_num_min=min(config.bond.min_total_volume_usd, config.hype.min_total_volume_usd),
    )
    storage = LocalStorage(args.data_dir)
    scan(markets, clients.fetch_book, storage, config, now)
    for key in storage.list(f"candidates/date={now:%Y-%m-%d}/"):
        print(args.data_dir / key)


if __name__ == "__main__":
    main()
