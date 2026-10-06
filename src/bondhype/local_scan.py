"""Run one scan against live data, writing candidates to a local directory."""

import argparse
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bondhype import clients
from bondhype.config import load_config
from bondhype.deepseek import DeepSeekClient
from bondhype.llm import LLMSetup, load_prompt
from bondhype.scan import scan
from bondhype.storage import LocalStorage


def _load_env(path: Path) -> None:
    """Read KEY=VALUE lines from a .env file into the environment, without overriding."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--prompts-dir", type=Path, default=Path("prompts"))
    parser.add_argument("--no-llm", action="store_true", help="baseline arm only")
    args = parser.parse_args()

    _load_env(Path(".env"))
    config = load_config(args.config)
    llm = None
    if not args.no_llm:
        llm = LLMSetup(
            client=DeepSeekClient(os.environ["DEEPSEEK_API_KEY"]),
            reject=load_prompt(args.prompts_dir, config.llm.reject_prompt),
            buy=load_prompt(args.prompts_dir, config.llm.buy_prompt),
        )
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
    scan(markets, clients.fetch_book, storage, config, now, llm=llm)
    for key in storage.list(f"candidates/date={now:%Y-%m-%d}/"):
        print(args.data_dir / key)


if __name__ == "__main__":
    main()
