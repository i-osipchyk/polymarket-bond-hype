import json
import logging
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from bondhype.arms import ARMS
from bondhype.config import load_config
from bondhype.llm import Completion, LLMSetup, Prompt
from bondhype.portfolio import positions_prefix
from bondhype.pricing import load_pricing
from bondhype.scan import scan
from bondhype.storage import LocalStorage
from builders import NOW

PRICING = load_pricing(Path(__file__).parent.parent / "deepseek_pricing.yaml")

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
            return Completion("garbage")
        return Completion(verdict_json(said))


def setup(reject_says: str, buy_says: str) -> tuple[LLMSetup, PromptAwareClient]:
    client = PromptAwareClient(reject_says, buy_says)
    return LLMSetup(client=client, reject=REJECT_PROMPT, buy=BUY_PROMPT, pricing=PRICING), client


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


def several_markets(n):
    """n copies of the bond fixture with distinct ids, events and tokens, plus their books."""
    market, books = bond_market_and_books()
    old_yes, old_no = json.loads(market["clobTokenIds"])
    raws, by_token = [], {}
    for i in range(n):
        yes, no = f"yes-{i}", f"no-{i}"
        raws.append(
            market
            | {"id": f"9{i}", "clobTokenIds": json.dumps([yes, no]), "events": [{"id": f"ev{i}"}]}
        )
        by_token |= {yes: books[old_yes], no: books[old_no]}
    return raws, by_token


class RendezvousClient:
    """Every call waits until `parties` calls are in flight; sequential code can never get there."""

    def __init__(self, parties):
        self._barrier = threading.Barrier(parties, timeout=2)

    def complete(self, *, model, system, user):
        self._barrier.wait()
        return Completion(verdict_json("buy"))


def test_llm_reviews_run_concurrently_and_the_outcome_is_unchanged(tmp_path):
    storage = LocalStorage(tmp_path)
    raws, books = several_markets(2)  # 2 markets x 2 prompts = 4 calls that must overlap
    llm = LLMSetup(
        client=RendezvousClient(parties=4), reject=REJECT_PROMPT, buy=BUY_PROMPT, pricing=PRICING
    )

    scan(raws, lambda token: books[token], storage, CONFIG, NOW, llm=llm)

    for arm in ARMS:
        assert len(storage.list(positions_prefix(arm, "bond"))) == 2
    assert len(storage.list("llm_calls/")) == 4


def test_worker_count_comes_from_config(tmp_path):
    storage = LocalStorage(tmp_path)
    raws, books = several_markets(2)
    one_worker = CONFIG.model_copy(update={"llm": CONFIG.llm.model_copy(update={"max_workers": 1})})
    llm = LLMSetup(
        client=RendezvousClient(parties=2), reject=REJECT_PROMPT, buy=BUY_PROMPT, pricing=PRICING
    )

    with pytest.raises(threading.BrokenBarrierError):  # one worker can never meet at the barrier
        scan(raws, lambda token: books[token], storage, one_worker, NOW, llm=llm)


def test_the_run_logs_candidates_llm_calls_and_opened_positions(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    llm, _ = setup("buy", "reject")

    market = run_scan(LocalStorage(tmp_path), llm)

    log = caplog.text
    assert "fetched and evaluated 1 markets" in log
    assert f"candidate {market['id']} bond NO" in log
    assert "reviewing 1 candidates (2 LLM calls)" in log
    assert f"llm {REJECT_PROMPT.id} {market['id']} -> buy" in log
    assert f"llm {BUY_PROMPT.id} {market['id']} -> reject" in log
    assert f"opened prompt_reject/bond NO on {market['id']}" in log
