"""Every storage call is an S3 request, so cost is behaviour: these tests cap the calls per run."""

import copy
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

from bondhype.config import load_config
from bondhype.deepseek import Completion
from bondhype.ledger import load_ledger, save_checkpoint
from bondhype.llm import LLMSetup, Prompt
from bondhype.portfolio import position_key
from bondhype.pricing import load_pricing
from bondhype.reporting import build_report, check_heartbeat
from bondhype.scan import scan
from bondhype.settlement import resolution_key
from bondhype.track import track
from builders import NOW, make_position, make_resolution
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


def settled_history(storage, n):
    """n old baseline/bond trades, all resolved, so they cost reads but block no entry."""
    for i in range(n):
        position = make_position(f"old{i}", opened_at=NOW - timedelta(days=30))
        storage.put(position_key("baseline", "bond", position.market_id), position.to_json())
        resolution = make_resolution(position)
        storage.put(resolution_key("baseline", "bond", position.market_id), resolution.to_json())


def record_reads(storage):
    return Counter(k for k in storage.got if k.startswith("arms/"))


def test_a_scan_reads_each_stored_record_at_most_once_however_many_it_opens(tmp_path):
    storage = CountingStorage(tmp_path)
    settled_history(storage, 50)
    markets, books = bond_markets(10)
    storage.reset()

    scan(markets, books.__getitem__, storage, CONFIG, NOW)

    assert len(storage.list("arms/baseline/bond/positions/")) == 60  # all ten opened
    assert max(record_reads(storage).values()) == 1
    assert storage.calls["exists"] == 0


def test_the_next_scan_opens_more_without_reading_any_stored_record_again(tmp_path):
    storage = CountingStorage(tmp_path)
    settled_history(storage, 50)
    markets, books = bond_markets(20)
    scan(markets[:10], books.__getitem__, storage, CONFIG, NOW)
    storage.reset()

    scan(markets[10:], books.__getitem__, storage, CONFIG, NOW + timedelta(minutes=15))

    assert len(storage.list("arms/baseline/bond/positions/")) == 70
    assert record_reads(storage) == Counter()


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


def lists_in_a_rescan(storage, n):
    markets, books = bond_markets(n)
    scan(markets, books.__getitem__, storage, CONFIG, NOW, llm=REJECTING)
    storage.reset()
    scan(markets, books.__getitem__, storage, CONFIG, NOW + timedelta(minutes=15), llm=REJECTING)
    return storage.calls["list"]


def test_cooldown_checks_cost_the_same_listings_for_ten_candidates_or_thirty(tmp_path):
    few = lists_in_a_rescan(CountingStorage(tmp_path / "few"), 10)
    many = lists_in_a_rescan(CountingStorage(tmp_path / "many"), 30)
    assert few == many


def open_overdue_market(market_id):
    raw = json.loads((LIVE / "settle_overdue_open.json").read_text())
    return raw | {"id": market_id}


def test_the_tracker_reads_no_stored_record_the_last_checkpoint_holds(tmp_path):
    storage = CountingStorage(tmp_path)
    settled_history(storage, 50)
    for i in range(3):
        position = make_position(f"open{i}")
        storage.put(position_key("baseline", "bond", position.market_id), position.to_json())
    save_checkpoint(storage, load_ledger(storage, NOW), NOW)
    storage.reset()

    summary = track(storage, open_overdue_market, lambda token: None, NOW + timedelta(minutes=5))

    assert summary.snapshotted == 3
    assert record_reads(storage) == Counter()
    assert storage.calls["exists"] == 0


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


def test_the_report_reads_no_stored_record_the_last_checkpoint_holds(tmp_path):
    storage = CountingStorage(tmp_path)
    settled_history(storage, 50)
    save_checkpoint(storage, load_ledger(storage, NOW), NOW)
    storage.reset()

    report = build_report(storage, CONFIG, NOW + timedelta(hours=1))

    assert report.entry("baseline", "bond").stats.resolved.n_trades == 50
    assert not [k for k in record_reads(storage) if "/positions/" in k or "/resolutions/" in k]
