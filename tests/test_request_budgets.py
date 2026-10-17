"""Every storage call is an S3 request, so cost is behaviour: these tests cap the calls per run.

Each job reads the trades table (and the scanner and report the decisions table) once and writes
each at most once, however much history the tables hold.
"""

import copy
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest

from bondhype.config import load_config
from bondhype.decisions import DecisionLog, save_decisions
from bondhype.deepseek import Completion
from bondhype.ledger import Ledger, save_ledger
from bondhype.llm import LLMSetup, Prompt
from bondhype.overdue import run_overdue
from bondhype.pricing import load_pricing
from bondhype.reporting import build_report, check_heartbeat
from bondhype.scan import scan
from bondhype.track import track
from builders import NOW, make_decision, make_position, make_resolution
from counting import CountingStorage

CONFIG = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")
LIVE = Path(__file__).parent / "fixtures" / "live"
YES_SIDE_BOOK = {
    "bids": [{"price": "0.07", "size": "100"}],
    "asks": [{"price": "0.08", "size": "100"}],
}


def bond_markets(n):
    """n copies of a passing bond market, each its own market, event and tokens."""
    fixture = json.loads((LIVE / "bond_sports_tight.json").read_text())
    markets, books = [], {}
    for i in range(n):
        market = copy.deepcopy(fixture["gamma_market"])
        market["id"] = f"m{i}"
        market["events"][0]["id"] = f"e{i}"
        market["clobTokenIds"] = json.dumps([f"yes{i}", f"no{i}"])
        books[f"yes{i}"], books[f"no{i}"] = YES_SIDE_BOOK, fixture["clob_book"]
        markets.append(market)
    return markets, books


def with_history(storage, n, open_positions=0):
    """n old baseline/bond trades, all resolved, plus some still open, and n old decisions."""
    ledger = Ledger(changed=True)  # stored even when empty: the table exists from migration on
    for i in range(n):
        position = make_position(f"old{i}", opened_at=NOW - timedelta(days=30))
        ledger.add_position(position)
        ledger.add_resolution(make_resolution(position))
    for i in range(open_positions):
        ledger.add_position(make_position(f"open{i}"))
    save_ledger(storage, ledger)
    old = [make_decision(f"old{i}", scan_at=NOW - timedelta(days=30)) for i in range(n)]
    save_decisions(storage, DecisionLog(old, changed=True))
    storage.reset()


class RejectingClient:
    def complete(self, *, model, system, user):
        verdict = {"verdict": "reject", "risk_flags": [], "confidence": 3, "reason": "r"}
        return Completion(json.dumps(verdict))


REJECTING = LLMSetup(
    client=RejectingClient(),
    reject=Prompt(id="reject_v1", system="reject-by-default"),
    buy=Prompt(id="buy_v1", system="buy-by-default"),
    pricing=load_pricing(Path(__file__).parent.parent / "deepseek_pricing.yaml"),
)

SCAN_BUDGET = Counter(read_versioned=2, write_versioned=2, put=2)  # + llm audit + candidates


@pytest.mark.parametrize("history,markets", [(0, 1), (50, 10), (500, 30)])
def test_a_scan_costs_the_same_requests_however_much_history_or_how_many_candidates(
    tmp_path, history, markets
):
    storage = CountingStorage(tmp_path)
    with_history(storage, history)
    raws, books = bond_markets(markets)

    scan(raws, books.__getitem__, storage, CONFIG, NOW, llm=REJECTING)

    assert storage.calls == SCAN_BUDGET


def test_a_rescan_with_every_market_in_cooldown_costs_no_more(tmp_path):
    storage = CountingStorage(tmp_path)
    raws, books = bond_markets(10)
    scan(raws, books.__getitem__, storage, CONFIG, NOW, llm=REJECTING)
    storage.reset()

    scan(raws, books.__getitem__, storage, CONFIG, NOW + timedelta(hours=1), llm=REJECTING)

    # nothing new to trade or review: no audit file and no decisions write
    assert storage.calls == Counter(read_versioned=2, put=1)


def open_overdue_market(market_id):
    raw = json.loads((LIVE / "settle_overdue_open.json").read_text())
    return raw | {"id": market_id}


def test_the_tracker_reads_the_trades_table_once_and_writes_one_price_file(tmp_path):
    storage = CountingStorage(tmp_path)
    with_history(storage, 50, open_positions=3)

    summary = track(storage, open_overdue_market, lambda token: None, NOW + timedelta(minutes=5))

    assert summary.snapshotted == 3
    assert storage.calls == Counter(read_versioned=1, put=1)  # nothing settled: no table write


def test_the_overdue_job_reads_and_writes_the_trades_table_once(tmp_path):
    storage = CountingStorage(tmp_path)
    with_history(storage, 50, open_positions=3)

    run_overdue(storage, open_overdue_market, NOW + timedelta(days=30))

    assert storage.calls == Counter(read_versioned=1, write_versioned=1)


def store_scan(storage, when):
    key = f"candidates/date={when:%Y-%m-%d}/scan={when:%Y-%m-%dT%H:%M:%SZ}.jsonl"
    storage.put(key, b'{"market_id": "m", "selected": null, "attempts": []}\n')


def test_the_heartbeat_lists_only_todays_and_yesterdays_scans(tmp_path):
    storage = CountingStorage(tmp_path)
    for days_ago in range(30):
        store_scan(storage, NOW - timedelta(days=days_ago, minutes=10))
    storage.reset()

    assert check_heartbeat(storage, CONFIG, NOW).ok
    assert storage.listed == ["candidates/date=2026-10-04/", "candidates/date=2026-10-03/"]


def test_the_heartbeat_alerts_when_the_last_scan_is_days_old(tmp_path):
    storage = CountingStorage(tmp_path)
    store_scan(storage, NOW - timedelta(days=3))

    beat = check_heartbeat(storage, CONFIG, NOW)

    assert not beat.ok
    assert "no scan output" in beat.message.lower()


def test_the_report_reads_both_tables_once_and_only_todays_scan_files(tmp_path):
    storage = CountingStorage(tmp_path)
    with_history(storage, 50)
    for hours_ago in (1, 2, 30):
        store_scan(storage, NOW - timedelta(hours=hours_ago))
    storage.reset()

    report = build_report(storage, CONFIG, NOW)

    assert report.entry("baseline", "bond").stats.resolved.n_trades == 50
    assert storage.calls == Counter(read_versioned=2, list=1, get=2)
