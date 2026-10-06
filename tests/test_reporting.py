import json
from datetime import timedelta
from pathlib import Path

import pytest

from bondhype.config import load_config
from bondhype.overdue import overdue_key
from bondhype.portfolio import position_key
from bondhype.reporting import build_report, check_heartbeat, format_report
from bondhype.settlement import resolution_key
from bondhype.storage import LocalStorage
from builders import NOW, make_overdue, make_position, make_resolution

CONFIG = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path)


def _store_position(storage, position):
    storage.put(
        position_key(position.arm, position.strategy, position.market_id), position.to_json()
    )


def _store_scan(storage, when, selections):
    """One candidates file; each selection is a (strategy, side) pair or None."""
    lines = [
        json.dumps(
            {
                "market_id": f"c{i}",
                "selected": s and {"strategy": s[0], "side": s[1]},
                "attempts": [],
            }
        )
        for i, s in enumerate(selections)
    ]
    key = f"candidates/date={when:%Y-%m-%d}/scan={when:%Y-%m-%dT%H:%M:%SZ}.jsonl"
    storage.put(key, ("\n".join(lines) + "\n").encode())


def test_report_counts_todays_candidates_and_trades_per_arm_and_strategy(storage):
    _store_scan(storage, NOW - timedelta(hours=2), [("bond", "NO"), ("hype", "NO"), None])
    _store_scan(storage, NOW - timedelta(hours=1), [("bond", "YES")])
    _store_scan(storage, NOW - timedelta(days=1), [("bond", "NO")])  # yesterday: excluded
    _store_position(storage, make_position("t1"))
    _store_position(storage, make_position("t2"))
    _store_position(storage, make_position("old", opened_at=NOW - timedelta(days=1)))
    _store_position(storage, make_position("h1", arm="prompt_buy", strategy="hype"))

    report = build_report(storage, CONFIG, NOW)

    baseline_bond = report.entry("baseline", "bond")
    assert (baseline_bond.candidates_today, baseline_bond.trades_today) == (2, 2)
    assert report.entry("baseline", "hype").candidates_today == 1
    assert report.entry("prompt_buy", "hype").trades_today == 1
    assert report.entry("mix_or", "bond").trades_today == 0
    assert len(report.entries) == 10  # 5 arms x 2 strategies, even when empty


def _store_resolution(storage, resolution):
    key = resolution_key(resolution.arm, resolution.strategy, resolution.market_id)
    storage.put(key, resolution.to_json())


def test_report_carries_cumulative_stats_and_the_gate_verdict_per_arm(storage):
    opened = NOW - timedelta(days=10)
    for i in range(100):
        position = make_position(f"w{i}", opened_at=opened)
        _store_position(storage, position)
        # all resolved 5 days after opening, except one that resolves today
        _store_resolution(storage, make_resolution(position, days_to_resolve=10 if i == 0 else 5))

    report = build_report(storage, CONFIG, NOW)

    baseline = report.entry("baseline", "bond")
    assert baseline.resolutions_today == 1
    assert baseline.stats.resolved.n_trades == 100
    assert baseline.verdict.passed
    idle = report.entry("prompt_reject", "bond")
    assert idle.resolutions_today == 0
    assert not idle.verdict.passed  # no trades: fails the count and cannot beat baseline
    assert report.entry("baseline", "hype").stats.resolved.n_trades == 0


def _store_overdue(storage, position, when, **overrides):
    report = make_overdue(position, **overrides)
    storage.put(
        overdue_key(position.arm, position.strategy, position.market_id, when), report.to_json()
    )


def test_report_lists_overdue_positions_with_their_latest_status_and_counts_them_as_losses(storage):
    stuck = make_position("stuck", opened_at=NOW - timedelta(days=12))
    _store_position(storage, stuck)
    _store_overdue(storage, stuck, NOW - timedelta(days=1), days_overdue=2.0, uma_status="proposed")
    _store_overdue(
        storage, stuck, NOW, days_overdue=3.0, uma_status="disputed", uma_statuses=("disputed",)
    )
    settled = make_position("settled", opened_at=NOW - timedelta(days=12))
    _store_position(storage, settled)
    _store_resolution(storage, make_resolution(settled, days_to_resolve=11))
    _store_overdue(storage, settled, NOW - timedelta(days=1))  # stale: it resolved afterwards

    entry = build_report(storage, CONFIG, NOW).entry("baseline", "bond")

    assert [o.market_id for o in entry.overdue] == ["stuck"]
    assert entry.overdue[0].days_overdue == 3.0
    assert entry.overdue[0].was_disputed
    assert entry.stats.resolved.n_trades == 1
    assert entry.stats.conservative.n_trades == 2
    assert entry.stats.n_overdue == 1


def _store_llm_call(storage, when, market, prompt_id, verdict, strategy="bond"):
    key = (
        f"llm_calls/date={when:%Y-%m-%d}/market={market}/{prompt_id}/"
        f"scan={when:%Y-%m-%dT%H:%M:%SZ}.json"
    )
    record = {
        "prompt_id": prompt_id,
        "user": json.dumps({"strategy": strategy}),
        "verdict": {"verdict": verdict},
    }
    storage.put(key, json.dumps(record).encode())


def test_report_gives_each_llm_arm_its_prompts_error_rate_for_today(storage):
    for i, verdict in enumerate(["buy", "reject", "reject", "error"]):
        _store_llm_call(storage, NOW, f"r{i}", "reject_v1", verdict)
    for i, verdict in enumerate(["buy", "reject"]):
        _store_llm_call(storage, NOW, f"b{i}", "buy_v1", verdict)
    _store_llm_call(storage, NOW, "x", "buy_v1", "error", strategy="hype")
    _store_llm_call(storage, NOW - timedelta(days=1), "old", "reject_v1", "error")

    report = build_report(storage, CONFIG, NOW)

    assert report.entry("prompt_reject", "bond").llm_error_rate == pytest.approx(1 / 4)
    assert report.entry("prompt_buy", "bond").llm_error_rate == 0.0
    assert report.entry("mix_and", "bond").llm_error_rate == pytest.approx(1 / 6)
    assert report.entry("mix_or", "hype").llm_error_rate == 1.0
    assert report.entry("prompt_reject", "hype").llm_error_rate is None  # no calls today
    assert report.entry("baseline", "bond").llm_error_rate is None  # makes no LLM calls


def _arm_block(section, arm):
    """The header line `arm: ...` plus its indented detail lines."""
    lines = section.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{arm}:"))
    end = next(
        (k for k in range(start + 1, len(lines)) if not lines[k].startswith(" ")), len(lines)
    )
    return "\n".join(lines[start:end])


def test_formatted_report_shows_both_views_gate_status_and_overdue_positions(storage):
    opened = NOW - timedelta(days=12)
    for i in range(100):
        position = make_position(f"w{i}", opened_at=opened)
        _store_position(storage, position)
        _store_resolution(storage, make_resolution(position, days_to_resolve=5))
    stuck = make_position("stuck", opened_at=opened)
    _store_position(storage, stuck)
    _store_overdue(
        storage, stuck, NOW, days_overdue=3.0, uma_status="disputed", uma_statuses=("disputed",)
    )
    _store_scan(storage, NOW - timedelta(hours=1), [("bond", "NO")])

    text = format_report(build_report(storage, CONFIG, NOW))
    bond = text.split("HYPE")[0]
    baseline = _arm_block(bond, "baseline")

    assert max(len(line) for line in text.splitlines()) <= 48
    assert "2026-10-04" in text and CONFIG.version in text
    assert "resolved n=100 EV +0.54" in baseline  # 0.55 less ~0.006 capital cost
    assert "conservative n=101 EV +0.44" in baseline  # overdue position counted as a total loss
    assert "win 100.0% vs break-even 94.5%" in baseline
    assert "cand 1" in baseline
    assert "gate PASS" in baseline
    assert "overdue stuck 3.0d disputed" in baseline
    assert "gate FAIL" in _arm_block(bond, "prompt_reject")
    assert "exploratory" in text.lower()


def test_heartbeat_is_ok_when_the_latest_scan_is_recent(storage):
    _store_scan(storage, NOW - timedelta(hours=3), [None])
    _store_scan(storage, NOW - timedelta(minutes=10), [None])

    assert check_heartbeat(storage, CONFIG, NOW).ok


def test_heartbeat_alerts_when_the_latest_scan_is_older_than_the_limit(storage):
    _store_scan(storage, NOW - timedelta(minutes=50), [None])

    beat = check_heartbeat(storage, CONFIG, NOW)

    assert not beat.ok
    assert "50 min" in beat.message


def test_heartbeat_alerts_when_no_scan_output_exists(storage):
    beat = check_heartbeat(storage, CONFIG, NOW)

    assert not beat.ok
    assert "no scan output" in beat.message.lower()
