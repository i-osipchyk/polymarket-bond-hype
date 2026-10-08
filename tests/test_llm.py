import json
import logging
from pathlib import Path

import pytest

from bondhype.llm import Completion, LLMError, Prompt, Usage, Verdict, review
from bondhype.pricing import load_pricing
from bondhype.storage import LocalStorage
from builders import NOW

PRICING = load_pricing(Path(__file__).parent.parent / "deepseek_pricing.yaml")

PROMPT = Prompt(id="reject_v1", system="Find a reason to reject.")
SNAPSHOT = {"question": "Will X happen?", "side": "NO", "ask": 0.93}
BUY = json.dumps(
    {"verdict": "buy", "risk_flags": [], "confidence": 4, "reason": "Outcome is settled."}
)


class FakeClient:
    """Plays back canned replies (text or Completion); an Exception instance is raised instead."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, *, model, system, user):
        self.calls.append({"model": model, "system": system, "user": user})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, Completion) else Completion(text=reply)


def run(client, tmp_path, model="deepseek-flash"):
    return review(
        SNAPSHOT,
        PROMPT,
        client,
        model=model,
        pricing=PRICING,
        storage=LocalStorage(tmp_path),
        market_id="m1",
        config_version="v-test",
        now=NOW,
    )


def test_valid_reply_is_parsed_into_a_verdict(tmp_path):
    verdict = run(FakeClient(BUY), tmp_path)

    assert verdict == Verdict(
        verdict="buy", risk_flags=(), confidence=4, reason="Outcome is settled."
    )


def test_invalid_json_is_retried_once_and_a_valid_retry_is_used(tmp_path):
    client = FakeClient("not json at all", BUY)

    verdict = run(client, tmp_path)

    assert verdict.verdict == "buy"
    assert len(client.calls) == 2


def test_two_invalid_replies_fail_closed_as_error_without_a_third_call(tmp_path):
    client = FakeClient("not json", "still not json", BUY)

    verdict = run(client, tmp_path)

    assert verdict.verdict == "error"
    assert verdict.confidence is None
    assert len(client.calls) == 2


def reply(**overrides) -> str:
    body = {"verdict": "reject", "risk_flags": [], "confidence": 3, "reason": "r"} | overrides
    return json.dumps(body)


@pytest.mark.parametrize(
    "bad",
    [
        reply(risk_flags=["made_up_flag"]),
        reply(confidence=0),
        reply(confidence=6),
        reply(confidence=3.5),
        reply(confidence="4"),
        reply(verdict="maybe"),
        reply(verdict="error"),
        reply(reason=""),
        reply(extra_field=1),
        json.dumps(["buy"]),
    ],
)
def test_schema_violations_are_never_accepted_and_fail_closed(bad, tmp_path):
    verdict = run(FakeClient(bad, bad), tmp_path)

    assert verdict.verdict == "error"


def test_every_fixed_vocabulary_flag_is_accepted(tmp_path):
    flags = [
        "ambiguous_resolution",
        "scheduled_catalyst",
        "dispute_risk",
        "thin_book",
        "insider_risk",
        "already_decided",
    ]

    verdict = run(FakeClient(reply(risk_flags=flags)), tmp_path)

    assert verdict.risk_flags == tuple(flags)


def test_provider_error_is_retried_once_then_fails_closed(tmp_path):
    client = FakeClient(LLMError("HTTP 503"), LLMError("HTTP 503"), BUY)

    verdict = run(client, tmp_path)

    assert verdict.verdict == "error"
    assert len(client.calls) == 2


def test_provider_error_followed_by_a_valid_reply_recovers(tmp_path):
    verdict = run(FakeClient(LLMError("timeout"), BUY), tmp_path)

    assert verdict.verdict == "buy"


def stored_calls(tmp_path) -> list[dict]:
    storage = LocalStorage(tmp_path)
    return [json.loads(storage.get(key)) for key in storage.list("llm_calls/")]


def test_call_is_stored_with_full_prompt_input_raw_output_and_versions(tmp_path):
    run(FakeClient(BUY), tmp_path)

    (record,) = stored_calls(tmp_path)
    assert record["market_id"] == "m1"
    assert record["prompt_id"] == "reject_v1"
    assert record["model"] == "deepseek-flash"
    assert record["config_version"] == "v-test"
    assert record["system"] == "Find a reason to reject."
    assert json.loads(record["user"]) == SNAPSHOT
    assert [(a["output"], a["error"]) for a in record["attempts"]] == [(BUY, None)]
    assert record["verdict"]["verdict"] == "buy"


def test_failed_attempts_and_the_error_verdict_are_stored_too(tmp_path):
    run(FakeClient(LLMError("HTTP 503"), "garbage"), tmp_path)

    (record,) = stored_calls(tmp_path)
    assert record["verdict"]["verdict"] == "error"
    assert (record["attempts"][0]["output"], record["attempts"][0]["error"]) == (None, "HTTP 503")
    assert record["attempts"][1]["output"] == "garbage"
    assert record["attempts"][1]["error"]


def test_the_same_call_is_stored_under_a_deterministic_date_partitioned_key(tmp_path):
    run(FakeClient(BUY), tmp_path)
    (key,) = LocalStorage(tmp_path).list("llm_calls/")

    assert key.startswith("llm_calls/date=2026-10-04/")
    assert "m1" in key and "reject_v1" in key


def test_a_retried_scan_reuses_the_stored_verdict_instead_of_calling_again(tmp_path):
    first = run(FakeClient(BUY), tmp_path)
    second_client = FakeClient(reply(verdict="reject"))

    second = run(second_client, tmp_path)

    assert second == first
    assert second_client.calls == []


def test_a_new_call_is_logged_with_its_verdict_and_duration(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    run(FakeClient(BUY), tmp_path)

    assert "llm reject_v1 m1 -> buy in " in caplog.text


def test_a_reused_review_is_logged_as_cached(tmp_path, caplog):
    run(FakeClient(BUY), tmp_path)
    caplog.set_level(logging.INFO)
    caplog.clear()

    run(FakeClient(), tmp_path)

    assert "llm reject_v1 m1 -> buy (cached)" in caplog.text


def test_failed_attempts_are_logged_as_warnings_before_the_error_verdict(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    run(FakeClient(LLMError("timeout"), "garbage"), tmp_path)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "llm reject_v1 m1 attempt 1 failed" in warnings[0] and "timeout" in warnings[0]
    assert "llm reject_v1 m1 -> error" in caplog.text


def test_configured_prompts_exist_and_state_the_whole_output_contract():
    from pathlib import Path

    from bondhype.config import load_config
    from bondhype.llm import RISK_FLAGS, load_prompt

    root = Path(__file__).parent.parent
    config = load_config(root / "config" / "config.yaml")

    for prompt_id in (config.llm.reject_prompt, config.llm.buy_prompt):
        prompt = load_prompt(root / "prompts", prompt_id)
        assert prompt.id == prompt_id
        for flag in RISK_FLAGS:
            assert flag in prompt.system
        for field in ("verdict", "risk_flags", "confidence", "reason"):
            assert field in prompt.system


USAGE = Usage(cache_hit_tokens=350, cache_miss_tokens=150, output_tokens=80)
USAGE_COST = 0.00007155  # off-peak at NOW (12:00 UTC): (350 * 0.003 + 150 * 0.15 + 80 * 0.60) / 1e6


def test_each_attempt_stores_its_token_usage_and_cost_and_the_record_stores_the_total(tmp_path):
    run(FakeClient(Completion(BUY, USAGE)), tmp_path)

    (record,) = stored_calls(tmp_path)
    (attempt,) = record["attempts"]
    assert attempt["usage"] == {
        "cache_hit_tokens": 350,
        "cache_miss_tokens": 150,
        "output_tokens": 80,
    }
    assert attempt["cost_usd"] == pytest.approx(USAGE_COST)
    assert record["cost_usd"] == pytest.approx(USAGE_COST)


def test_a_rejected_reply_is_still_billed_and_a_failed_call_is_not(tmp_path):
    client = FakeClient(LLMError("HTTP 402"), Completion("garbage", USAGE))

    run(client, tmp_path)

    (record,) = stored_calls(tmp_path)
    failed, garbled = record["attempts"]
    assert failed["usage"] is None and failed["cost_usd"] is None
    assert garbled["cost_usd"] == pytest.approx(USAGE_COST)
    assert record["cost_usd"] == pytest.approx(USAGE_COST)


def test_the_record_cost_adds_up_every_billed_attempt(tmp_path):
    run(FakeClient(Completion("garbage", USAGE), Completion(BUY, USAGE)), tmp_path)

    (record,) = stored_calls(tmp_path)
    assert record["cost_usd"] == pytest.approx(2 * USAGE_COST)


def test_a_model_without_a_price_still_gets_its_verdict_with_the_cost_left_blank(tmp_path):
    verdict = run(FakeClient(Completion(BUY, USAGE)), tmp_path, model="deepseek-mystery")

    (record,) = stored_calls(tmp_path)
    assert verdict.verdict == "buy"
    assert record["cost_usd"] is None
    assert record["attempts"][0]["usage"]["output_tokens"] == 80
