"""Send the daily report, a heartbeat check or a test alert to Telegram from a local run."""

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from bondhype.config import load_config
from bondhype.local_scan import _load_env
from bondhype.reporting import build_report, check_heartbeat, format_report
from bondhype.storage import ImmutableKey, LocalStorage
from bondhype.telegram import TelegramClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--test-alert", action="store_true", help="send a test message and exit")
    parser.add_argument("--heartbeat", action="store_true", help="alert only if scans went quiet")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()

    _load_env(Path(".env"))
    config = load_config(args.config)
    storage = LocalStorage(args.data_dir)
    now = datetime.now(UTC).replace(microsecond=0)

    if args.dry_run:
        send = print
    else:
        send = TelegramClient(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]).send

    if args.test_alert:
        send(f"Test alert from polymarket-bond-hype at {now:%Y-%m-%d %H:%M}Z")
    elif args.heartbeat:
        beat = check_heartbeat(storage, config, now)
        if not beat.ok:
            send(f"ALERT: {beat.message}")
        print(beat.message)
    else:
        text = format_report(build_report(storage, config, now))
        if not args.dry_run:
            try:
                storage.put(f"reports/date={now:%Y-%m-%d}/report.txt", text.encode())
            except ImmutableKey:
                pass  # today's report was already stored; the first one stays the record
        send(text)


if __name__ == "__main__":
    main()
