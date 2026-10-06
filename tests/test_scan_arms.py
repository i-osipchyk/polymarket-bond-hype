import json
from datetime import timedelta
from pathlib import Path

import pytest

from bondhype.arms import ARMS
from bondhype.config import load_config
from bondhype.llm import LLMSetup, Prompt
from bondhype.portfolio import positions_prefix
from bondhype.scan import scan
from bondhype.storage import LocalStorage
from builders import NOW

CONFIG = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")
LIVE = Path(__file__).parent / "fixtures" / "live"
REJECT_PROMPT = Prompt(id="reject_v1", system="reject-by-default")
BUY_PROMPT = Prompt(id="buy_v1", system="buy-by-default")

YES_SIDE_BOOK = {
    "bids": [{"price": "0.07", "size": "100"}],
    "asks": [{"price": "0.08", "size": "100"}],
}


def verdict_json(kind: str) -> str:
    return json.dumps({"verdict": kind, "risk_flags": [], "confidence": 3, "reason": "r"})


class PromptAwareClient:
    """Answers per system prompt, so each prompt can be given a different verdict."""

    def __init__(self, reject_says: str, buy_says: str):
        self.by_system = {"reject-by-default": reject_says, "buy-by-default": buy_says}
        self.calls = []

    def complete(self, *, model, system, user):
        self.calls.append({"system": system, "user": user})
        said = self.by_system[system]
        if said == "garbage":
            return "garbage"
        return verdict_json(said)


def setup(reject_says: str, buy_says: str) -> tuple[LLMSetup, PromptAwareClient]:
    client = PromptAwareClient(reject_says, buy_says)
    return LLMSetup(client=client, reject=REJECT_PROMPT, buy=BUY_PROMPT), client


def bond_market_and_books():
    fixture = json.loads((LIVE / "bond_sports_tight.json").read_text())
    market = fixture["gamma_market"]
    yes_token, no_token = json.loads(market["clobTokenIds"])
    books = {yes_token: YES_SIDE_BOOK, no_token: fixture["clob_book"]}
    return market, books


def run_scan(storage, llm, now=NOW):
    market, books = bond_market_and_books()
    scan([market], lambda token: books[token], storage, CONFIG, now, llm=llm)
    return market


def positions(storage, strategy="bond") -> dict[str, dict]:
    found = {}
    for arm in ARMS:
        keys = storage.list(positions_prefix(arm, strategy))
        if keys:
            (key,) = keys
            found[arm] = json.loads(storage.get(key))
    return found


def test_both_prompts_buy_opens_a_position_in_every_arm_at_the_same_entry_price(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, _ = setup("buy", "buy")

    run_scan(storage, llm)

    opened = positions(storage)
    assert set(opened) == set(ARMS)
    assert len({p["avg_price"] for p in opened.values()}) == 1
    assert {p["side"] for p in opened.values()} == {"NO"}
    assert {p["config_version"] for p in opened.values()} == {"2026-10-04.1"}


def test_split_verdicts_trade_only_the_arms_the_routing_table_allows(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, _ = setup("reject", "buy")

    run_scan(storage, llm)

    assert set(positions(storage)) == {"baseline", "prompt_buy", "mix_or"}


def test_llm_errors_fail_closed_so_only_baseline_trades(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, _ = setup("garbage", "garbage")

    run_scan(storage, llm)

    assert set(positions(storage)) == {"baseline"}


def test_without_an_llm_only_baseline_trades(tmp_path):
    storage = LocalStorage(tmp_path)

    run_scan(storage, None)

    assert set(positions(storage)) == {"baseline"}


def test_both_prompts_are_called_once_per_candidate_and_stored(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, client = setup("buy", "buy")

    run_scan(storage, llm)

    assert sorted(c["system"] for c in client.calls) == ["buy-by-default", "reject-by-default"]
    assert len(storage.list("llm_calls/")) == 2


def test_rejected_arms_are_in_cooldown_so_the_next_scan_does_not_call_the_llm_again(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, client = setup("reject", "reject")
    run_scan(storage, llm)
    calls_after_first_scan = len(client.calls)

    run_scan(storage, llm, now=NOW + timedelta(minutes=15))

    assert len(client.calls) == calls_after_first_scan


def test_after_the_cooldown_a_rejected_market_is_reviewed_again_and_can_enter(tmp_path):
    storage = LocalStorage(tmp_path)
    run_scan(storage, setup("reject", "reject")[0])

    later = NOW + timedelta(hours=25)
    run_scan(storage, setup("buy", "buy")[0], now=later)

    assert set(positions(storage)) == set(ARMS)


def test_an_arm_that_already_holds_the_market_is_not_charged_again(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, client = setup("buy", "buy")
    run_scan(storage, llm)
    calls = len(client.calls)

    run_scan(storage, llm, now=NOW + timedelta(minutes=15))

    assert len(client.calls) == calls
    assert all(len(storage.list(f"arms/{arm}/bond/positions/")) == 1 for arm in ARMS)


def test_a_retried_scan_is_idempotent(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, _ = setup("buy", "buy")
    run_scan(storage, llm)
    before = {key: storage.get(key) for key in storage.list("")}

    run_scan(storage, setup("reject", "reject")[0])

    assert {key: storage.get(key) for key in storage.list("")} == before


def test_llm_input_is_a_stored_snapshot_with_resolution_rules_and_filter_values(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, client = setup("buy", "buy")

    market = run_scan(storage, llm)

    snapshot = json.loads(client.calls[0]["user"])
    assert snapshot["question"] == market["question"]
    assert "will resolve to" in snapshot["resolution_rules"]
    assert snapshot["side"] == "NO"
    assert snapshot["values"]["ask"] == 0.93
    assert snapshot["values"]["spread"] == pytest.approx(0.01)
