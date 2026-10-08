import json
from datetime import UTC, datetime
from pathlib import Path

from bondhype.config import load_config
from bondhype.handlers import (
    run_alarm,
    run_heartbeat,
    run_overdue_job,
    run_report,
    run_scanner,
    run_tracker,
)
from bondhype.portfolio import position_key
from bondhype.runtime import Runtime
from bondhype.storage import LocalStorage
from test_settlement import load, position

CONFIG = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")
NOW = datetime(2026, 10, 7, 9, 15, tzinfo=UTC)


def _no_call(*args):
    raise AssertionError("unexpected call")


class Harness:
    """A Runtime over local storage whose network edges are fakes; `sent` collects Telegram text."""

    def __init__(self, tmp_path, markets=(), fetch_market=_no_call, fetch_book=_no_call):
        self.sent: list[str] = []
        self.storage = LocalStorage(tmp_path)
        self.runtime = Runtime(
            storage=self.storage,
            config=CONFIG,
            llm=None,
            send=self.sent.append,
            list_markets=lambda now: list(markets),
            fetch_market=fetch_market,
            fetch_book=fetch_book,
        )


def test_scanner_writes_the_scan_output_the_heartbeat_looks_for(tmp_path):
    h = Harness(tmp_path)

    run_scanner(h.runtime, NOW)

    assert h.storage.list("candidates/") == [
        "candidates/date=2026-10-07/scan=2026-10-07T09:15:00Z.jsonl"
    ]
    assert h.sent == []


def _store(storage, pos):
    storage.put(position_key(pos.arm, pos.strategy, pos.market_id), pos.to_json())


def test_tracker_settles_resolved_positions_and_stays_quiet_when_nothing_fails(tmp_path):
    resolved = load("settle_resolved_no")
    h = Harness(tmp_path, fetch_market=lambda market_id: resolved)
    _store(h.storage, position(resolved["id"]))

    run_tracker(h.runtime, datetime(2026, 11, 10, 12, 0, tzinfo=UTC))

    assert h.storage.list("arms/baseline/bond/resolutions/") == [
        f"arms/baseline/bond/resolutions/{resolved['id']}.json"
    ]
    assert h.sent == []


def test_tracker_alerts_on_a_failed_market_without_blocking_the_others(tmp_path):
    broken, resolved = load("settle_overdue_open"), load("settle_resolved_no")

    def fetch_market(market_id):
        if market_id == broken["id"]:
            raise OSError("gamma down")
        return resolved

    h = Harness(tmp_path, fetch_market=fetch_market)
    _store(h.storage, position(broken["id"]))
    _store(h.storage, position(resolved["id"]))

    run_tracker(h.runtime, datetime(2026, 11, 10, 12, 0, tzinfo=UTC))

    assert len(h.storage.list("arms/baseline/bond/resolutions/")) == 1
    (message,) = h.sent
    assert "tracker" in message.lower()
    assert broken["id"] in message and "gamma down" in message


def test_overdue_job_records_analyses_and_alerts_on_a_failed_market(tmp_path):
    broken, late = load("settle_disputed"), load("settle_overdue_open")

    def fetch_market(market_id):
        if market_id == broken["id"]:
            raise OSError("gamma down")
        return late

    h = Harness(tmp_path, fetch_market=fetch_market)
    _store(h.storage, position(broken["id"]))
    _store(h.storage, position(late["id"]))

    run_overdue_job(h.runtime, datetime(2026, 10, 3, 3, 59, tzinfo=UTC))

    assert len(h.storage.list("arms/baseline/bond/overdue/")) == 1
    (message,) = h.sent
    assert "overdue" in message.lower() and broken["id"] in message


def test_report_is_sent_and_stored_and_a_same_day_rerun_keeps_the_first_copy(tmp_path):
    h = Harness(tmp_path)

    run_report(h.runtime, NOW)
    first = h.storage.get("reports/date=2026-10-07/report.txt").decode()
    h.storage.put(
        "candidates/date=2026-10-07/scan=2026-10-07T10:00:00Z.jsonl",
        b'{"selected": {"strategy": "bond", "side": "NO"}}\n',
    )
    run_report(h.runtime, NOW.replace(hour=18))  # differs from the stored text; must not raise

    assert len(h.sent) == 2
    assert h.sent[0] == first
    assert h.sent[1] != first  # the later report saw the new candidate
    assert first.startswith("Daily report 2026-10-07")
    assert h.storage.get("reports/date=2026-10-07/report.txt").decode() == first


def test_heartbeat_alerts_when_scans_went_quiet_and_stays_silent_when_they_ran(tmp_path):
    h = Harness(tmp_path)

    run_heartbeat(h.runtime, NOW)  # nothing has ever been scanned
    assert len(h.sent) == 1 and h.sent[0].startswith("ALERT:")

    run_scanner(h.runtime, NOW)
    run_heartbeat(h.runtime, NOW.replace(minute=30))  # 30 minutes after the scan
    assert len(h.sent) == 1


def _sns_event(**alarm):
    return {"Records": [{"Sns": {"Message": json.dumps(alarm)}}]}


def test_cloudwatch_alarm_from_sns_is_forwarded_to_telegram(tmp_path):
    h = Harness(tmp_path)

    run_alarm(
        h.runtime,
        _sns_event(
            AlarmName="bondhype-scanner-errors",
            NewStateValue="ALARM",
            NewStateReason="Threshold Crossed: 1 datapoint (2.0) > 0",
        ),
    )
    run_alarm(
        h.runtime,
        _sns_event(AlarmName="bondhype-scanner-errors", NewStateValue="OK", NewStateReason="ok"),
    )

    assert h.sent[0].startswith("ALERT: bondhype-scanner-errors is ALARM")
    assert "Threshold Crossed" in h.sent[0]
    assert h.sent[1].startswith("RESOLVED: bondhype-scanner-errors")
