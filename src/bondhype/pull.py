"""Mirror stored results from S3 into a local directory for analysis."""

import argparse
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from bondhype.arms import ARMS
from bondhype.settlement import STRATEGIES
from bondhype.storage import LocalStorage, S3Storage, Storage


@dataclass(frozen=True)
class PullSummary:
    copied: int
    skipped: int


def default_prefixes(include_heavy: bool = False) -> list[str]:
    """Everything for analysis; raw books and price paths are large, so they are opt-in."""
    prefixes = ["markets/", "candidates/", "llm_calls/", "reports/", "state/"]
    heavy = ["books/"]
    for arm in ARMS:
        for strategy in STRATEGIES:
            base = f"arms/{arm}/{strategy}/"
            prefixes += [f"{base}positions/", f"{base}resolutions/", f"{base}overdue/"]
            heavy.append(f"{base}pricepath/")
    return prefixes + heavy if include_heavy else prefixes


def pull(
    source: Storage,
    dest: Storage,
    prefixes: list[str] | None = None,
    *,
    workers: int = 16,
    progress: bool = False,
) -> PullSummary:
    prefixes = default_prefixes() if prefixes is None else prefixes
    wanted = [key for prefix in prefixes for key in source.list(prefix)]
    # stored data is append-only, so a local copy is already final
    missing = [key for key in wanted if not dest.exists(key)]

    def copy(key: str) -> None:
        dest.put(key, source.get(key))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # consuming the iterator surfaces any download error
        for _ in tqdm(pool.map(copy, missing), total=len(missing), disable=not progress):
            pass
    return PullSummary(copied=len(missing), skipped=len(wanted) - len(missing))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=os.environ.get("BONDHYPE_BUCKET"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--prefix", action="append", help="repeatable; replaces the defaults")
    parser.add_argument("--workers", type=int, default=16, help="parallel downloads")
    parser.add_argument("--heavy", action="store_true", help="also pull raw books and price paths")
    args = parser.parse_args()
    if not args.bucket:
        parser.error("pass --bucket or set BONDHYPE_BUCKET")

    prefixes = args.prefix or default_prefixes(include_heavy=args.heavy)
    summary = pull(
        S3Storage(args.bucket),
        LocalStorage(args.data_dir),
        prefixes,
        workers=args.workers,
        progress=True,
    )
    print(f"copied {summary.copied}, already local {summary.skipped} -> {args.data_dir}")


if __name__ == "__main__":
    main()
