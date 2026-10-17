import json
import logging
import threading
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest

from bondhype.arms import ARMS
from bondhype.config import load_config
from bondhype.decisions import load_decisions
from bondhype.ledger import load_ledger, save_ledger
from bondhype.llm import Completion, LLMSetup, Prompt
from bondhype.pricing import load_pricing
from bondhype.scan import scan
from bondhype.storage import LocalStorage
from builders import NOW, make_position

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


def run_scan(storage, llm, now=NOW, ask=None, config=CONFIG):
    """`ask` re-prices the NO side (0.93 in the fixture) with a single deep level."""
    market, books = bond_market_and_books()
    if ask is not None:
        no_token = json.loads(market["clobTokenIds"])[1]
        books[no_token] = {
            "bids": [{"price": f"{ask - 0.01:.2f}", "size": "5000"}],
            "asks": [{"price": f"{ask:.2f}", "size": "5000"}],
        }
    scan([market], lambda token: books[token], storage, config, now, llm=llm)
    return market


def positions(storage, strategy="bond") -> dict[str, dict]:
    ledger = load_ledger(storage)
    found = {}
    for arm in ARMS:
        held = ledger.positions(arm, strategy)
        if held:
            (position,) = held
            found[arm] = asdict(position)
    return found


def llm_records(storage) -> list[dict]:
    """Every call record in the audit files, one file per scan attempt."""
    return [
        json.loads(line)
        for key in storage.list("llm_calls/")
        for line in storage.get(key).decode().splitlines()
    ]


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
    assert len(storage.list("llm_calls/")) == 1  # one audit file for the whole scan
    assert sorted(r["prompt_id"] for r in llm_records(storage)) == ["buy_v1", "reject_v1"]


def prompts_called(client) -> list[str]:
    return [call["system"] for call in client.calls]


def test_the_buy_prompt_runs_first_and_a_no_from_it_skips_the_reject_prompt(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, client = setup("buy", "reject")  # the reject prompt would have said buy

    run_scan(storage, llm)

    assert prompts_called(client) == ["buy-by-default"]
    assert [r["prompt_id"] for r in llm_records(storage)] == ["buy_v1"]
    assert set(positions(storage)) == {"baseline"}


def test_a_buy_from_the_buy_prompt_sends_the_market_to_the_reject_prompt_too(tmp_path):
    llm, client = setup("reject", "buy")

    run_scan(LocalStorage(tmp_path), llm)

    assert prompts_called(client) == ["buy-by-default", "reject-by-default"]


def test_buy_then_reject_veto_trades_only_the_buy_prompt_and_mix_or(tmp_path):
    storage = LocalStorage(tmp_path)

    run_scan(storage, setup("reject", "buy")[0])

    assert set(positions(storage)) == {"baseline", "prompt_buy", "mix_or"}


def test_buy_then_reject_buy_trades_every_arm(tmp_path):
    storage = LocalStorage(tmp_path)

    run_scan(storage, setup("buy", "buy")[0])

    assert set(positions(storage)) == set(ARMS)


def test_a_failed_buy_prompt_fails_closed_without_calling_the_reject_prompt(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, client = setup("buy", "garbage")

    run_scan(storage, llm)

    assert prompts_called(client) == ["buy-by-default"] * 2  # one retry, then give up
    assert set(positions(storage)) == {"baseline"}


def test_a_market_the_buy_prompt_declined_is_cached_for_every_llm_arm(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, client = setup("buy", "reject")
    run_scan(storage, llm)
    calls = len(client.calls)

    run_scan(storage, llm, now=NOW + timedelta(minutes=15))

    assert len(client.calls) == calls


def test_rejected_arms_are_in_cooldown_so_the_next_scan_does_not_call_the_llm_again(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, client = setup("reject", "reject")
    run_scan(storage, llm)
    calls_after_first_scan = len(client.calls)

    run_scan(storage, llm, now=NOW + timedelta(minutes=15))

    assert len(client.calls) == calls_after_first_scan


def test_a_rejected_market_is_not_reviewed_again_however_much_time_passes(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, client = setup("reject", "reject")
    run_scan(storage, llm)
    calls = len(client.calls)

    run_scan(storage, llm, now=NOW + timedelta(days=3))

    assert len(client.calls) == calls


def test_a_rejected_market_is_reviewed_again_once_its_ask_has_moved_3_cents_and_can_enter(tmp_path):
    storage = LocalStorage(tmp_path)
    run_scan(storage, setup("reject", "reject")[0])

    run_scan(storage, setup("buy", "buy")[0], now=NOW + timedelta(minutes=15), ask=0.90)

    assert set(positions(storage)) == set(ARMS)


@pytest.mark.parametrize("ask", [0.91, 0.95])
def test_a_move_of_under_3_cents_either_way_keeps_the_market_cached(tmp_path, ask):
    storage = LocalStorage(tmp_path)
    llm, client = setup("reject", "reject")
    run_scan(storage, llm)
    calls = len(client.calls)

    run_scan(storage, llm, now=NOW + timedelta(minutes=15), ask=ask)

    assert len(client.calls) == calls


def fill_event_cap(storage):
    """Gives every arm the maximum open positions in the fixture market's event."""
    market, _ = bond_market_and_books()
    event_id = market["events"][0]["id"]
    ledger = load_ledger(storage)
    for arm in ARMS:
        for i in range(CONFIG.portfolio.max_positions_per_event):
            ledger.add_position(make_position(f"seed-{i}", arm=arm, event_id=event_id))
    save_ledger(storage, ledger)


def held(storage, arm, strategy="bond") -> int:
    return len(load_ledger(storage).positions(arm, strategy))


def test_an_arm_whose_entry_is_refused_is_cached_like_a_rejection(tmp_path):
    storage = LocalStorage(tmp_path)
    fill_event_cap(storage)
    llm, client = setup("buy", "buy")
    run_scan(storage, llm)
    # every entry hit the event cap: only the seeded positions exist
    assert all(held(storage, arm) == 5 for arm in ARMS)
    calls = len(client.calls)

    run_scan(storage, llm, now=NOW + timedelta(minutes=15))

    assert len(client.calls) == calls


def test_a_refused_entry_is_reviewed_again_when_the_price_moves_3_cents(tmp_path):
    storage = LocalStorage(tmp_path)
    fill_event_cap(storage)
    llm, client = setup("buy", "buy")
    run_scan(storage, llm)
    calls = len(client.calls)

    run_scan(storage, llm, now=NOW + timedelta(minutes=15), ask=0.90)

    assert len(client.calls) == calls + 2


def test_a_refused_entry_is_logged_with_the_arm_and_the_reason(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    storage = LocalStorage(tmp_path)
    fill_event_cap(storage)

    run_scan(storage, setup("buy", "buy")[0])

    assert "entry refused prompt_buy/bond" in caplog.text
    assert "event_cap" in caplog.text


def test_an_arm_that_already_holds_the_market_is_not_charged_again(tmp_path):
    storage = LocalStorage(tmp_path)
    llm, client = setup("buy", "buy")
    run_scan(storage, llm)
    calls = len(client.calls)

    run_scan(storage, llm, now=NOW + timedelta(minutes=15))

    assert len(client.calls) == calls
    assert all(held(storage, arm) == 1 for arm in ARMS)


def test_a_retried_scan_keeps_its_positions_and_every_attempts_llm_spend(tmp_path):
    storage = LocalStorage(tmp_path)
    run_scan(storage, setup("buy", "reject")[0])  # the buy prompt says no: only baseline opens
    first = positions(storage)

    run_scan(storage, setup("buy", "reject")[0])  # same scan time: the retry calls again

    assert positions(storage) == first
    assert len(storage.list("llm_calls/")) == 2  # each attempt's calls and costs are kept
    (decision,) = load_decisions(storage).decisions  # the retry replaces its own row
    assert decision.scan_at == NOW


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
    raws, books = several_markets(2)  # each phase has 2 calls that must overlap
    llm = LLMSetup(
        client=RendezvousClient(parties=2), reject=REJECT_PROMPT, buy=BUY_PROMPT, pricing=PRICING
    )

    scan(raws, lambda token: books[token], storage, CONFIG, NOW, llm=llm)

    for arm in ARMS:
        assert held(storage, arm) == 2
    assert len(llm_records(storage)) == 4


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
    llm, _ = setup("reject", "buy")

    market = run_scan(LocalStorage(tmp_path), llm)

    log = caplog.text
    assert "fetched and evaluated 1 markets" in log
    assert f"candidate {market['id']} bond NO" in log
    assert "reviewing 1 candidates with the buy prompt" in log
    assert "1 passed the buy prompt, sending them to the reject prompt" in log
    assert f"llm {BUY_PROMPT.id} {market['id']} -> buy" in log
    assert f"llm {REJECT_PROMPT.id} {market['id']} -> reject" in log
    assert f"opened prompt_buy/bond NO on {market['id']}" in log


def test_each_reviewed_candidate_gets_a_decision_row_with_verdicts_costs_and_arm_outcomes(tmp_path):
    storage = LocalStorage(tmp_path)

    market = run_scan(storage, setup("reject", "buy")[0])

    (d,) = load_decisions(storage).decisions
    assert (d.scan_at, d.market_id, d.strategy, d.side, d.ask) == (
        NOW,
        market["id"],
        "bond",
        "NO",
        0.93,
    )
    assert (d.buy.prompt_id, d.buy.verdict.verdict) == ("buy_v1", "buy")
    assert (d.reject.prompt_id, d.reject.verdict.verdict) == ("reject_v1", "reject")
    assert d.outcomes == {
        "prompt_reject": "rejected",
        "prompt_buy": "opened",
        "mix_and": "rejected",
        "mix_or": "opened",
    }
    assert d.config_version == CONFIG.version and d.model == CONFIG.llm.model


def test_a_skipped_reject_prompt_is_recorded_as_skipped(tmp_path):
    storage = LocalStorage(tmp_path)

    run_scan(storage, setup("buy", "reject")[0])

    (d,) = load_decisions(storage).decisions
    assert d.reject.verdict.verdict == "skipped" and d.reject.cost_usd is None


def test_a_candidate_no_llm_arm_can_trade_gets_no_decision_row(tmp_path):
    storage = LocalStorage(tmp_path)
    run_scan(storage, setup("buy", "buy")[0])

    run_scan(storage, setup("buy", "buy")[0], now=NOW + timedelta(hours=1))

    assert [d.scan_at for d in load_decisions(storage).decisions] == [NOW]


def test_a_scan_reads_and_writes_each_table_once_and_writes_one_audit_file(tmp_path):
    from counting import CountingStorage

    storage = CountingStorage(tmp_path)
    run_scan(storage, setup("reject", "reject")[0])
    storage.reset()

    run_scan(storage, setup("buy", "buy")[0], now=NOW + timedelta(hours=1), ask=0.90)

    assert storage.calls == {"read_versioned": 2, "write_versioned": 2, "put": 2}
    assert sorted(storage.got) == ["state/decisions.csv", "state/trades.csv"]


class FailingSecondCall:
    """A good reply, then an unexpected crash (not an LLMError, so nothing catches it)."""

    def __init__(self):
        self.calls = 0

    def complete(self, *, model, system, user):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("boom")
        return Completion(verdict_json("reject"))


def test_the_calls_that_finished_are_still_saved_when_a_later_one_crashes(tmp_path):
    storage = LocalStorage(tmp_path)
    raws, books = several_markets(2)
    serial = CONFIG.model_copy(update={"llm": CONFIG.llm.model_copy(update={"max_workers": 1})})
    llm = LLMSetup(
        client=FailingSecondCall(), reject=REJECT_PROMPT, buy=BUY_PROMPT, pricing=PRICING
    )

    with pytest.raises(RuntimeError):
        scan(raws, lambda token: books[token], storage, serial, NOW, llm=llm)

    assert len(llm_records(storage)) == 1
