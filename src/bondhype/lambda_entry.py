"""AWS Lambda entry points: one per job, selected by the function's image command."""

import os
from datetime import UTC, datetime

from bondhype import handlers
from bondhype.runtime import Runtime, build_runtime

_runtime: Runtime | None = None


def _get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:  # reused across warm invocations
        _runtime = build_runtime(os.environ)
    return _runtime


def _scheduled_time(event: dict) -> datetime:
    """The EventBridge schedule time, so a retried invocation writes the same keys."""
    if "time" in event:
        return datetime.fromisoformat(event["time"]).astimezone(UTC)
    return datetime.now(UTC).replace(microsecond=0)


def scanner(event, context):
    handlers.run_scanner(_get_runtime(), _scheduled_time(event))


def tracker(event, context):
    handlers.run_tracker(_get_runtime(), _scheduled_time(event))


def overdue(event, context):
    handlers.run_overdue_job(_get_runtime(), _scheduled_time(event))


def report(event, context):
    handlers.run_report(_get_runtime(), _scheduled_time(event))


def heartbeat(event, context):
    handlers.run_heartbeat(_get_runtime(), _scheduled_time(event))


def alarm(event, context):
    handlers.run_alarm(_get_runtime(), event)
