"""Lambda job bodies. Each takes a Runtime and the run time; the entry points only wire them up."""

import json
from datetime import datetime

from bondhype.overdue import run_overdue
from bondhype.reporting import build_report, check_heartbeat, format_report
from bondhype.runtime import Runtime
from bondhype.scan import scan
from bondhype.storage import ImmutableKey
from bondhype.track import track


def run_scanner(runtime: Runtime, now: datetime) -> None:
    scan(
        runtime.list_markets(now),
        runtime.fetch_book,
        runtime.storage,
        runtime.config,
        now,
        llm=runtime.llm,
    )


def _alert_errors(runtime: Runtime, job: str, errors: list[tuple[str, str]]) -> None:
    if errors:
        lines = [f"ALERT: {job} failed for {len(errors)} market(s):"]
        lines += [f"  {market_id}: {error}" for market_id, error in errors]
        runtime.send("\n".join(lines))


def run_tracker(runtime: Runtime, now: datetime) -> None:
    summary = track(runtime.storage, runtime.fetch_market, runtime.fetch_book, now)
    _alert_errors(runtime, "tracker", summary.errors)


def run_overdue_job(runtime: Runtime, now: datetime) -> None:
    errors = run_overdue(runtime.storage, runtime.fetch_market, now)
    _alert_errors(runtime, "overdue", errors)


def run_report(runtime: Runtime, now: datetime) -> None:
    text = format_report(build_report(runtime.storage, runtime.config, now))
    try:
        runtime.storage.put(f"reports/date={now:%Y-%m-%d}/report.txt", text.encode())
    except ImmutableKey:
        pass  # a same-day rerun: the first stored report stays the record
    runtime.send(text)


def run_heartbeat(runtime: Runtime, now: datetime) -> None:
    beat = check_heartbeat(runtime.storage, runtime.config, now)
    if not beat.ok:
        runtime.send(f"ALERT: {beat.message}")


def run_alarm(runtime: Runtime, event: dict) -> None:
    """Forward CloudWatch alarm state changes delivered through SNS."""
    for record in event["Records"]:
        alarm = json.loads(record["Sns"]["Message"])
        name, state, reason = alarm["AlarmName"], alarm["NewStateValue"], alarm["NewStateReason"]
        if state == "OK":
            runtime.send(f"RESOLVED: {name}")
        else:
            runtime.send(f"ALERT: {name} is {state}\n{reason}")
