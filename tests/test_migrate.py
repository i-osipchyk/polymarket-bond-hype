import json
from datetime import timedelta

import pytest

from bondhype.decisions import Cooldowns, load_decisions
from bondhype.ledger import TRADES_KEY, load_ledger
from bondhype.migrate import MigrationError, migrate
from bondhype.portfolio import position_key
from bondhype.settlement import resolution_key
from bondhype.storage import LocalStorage
from builders import NOW, make_overdue, make_position, make_resolution
from test_decisions import CONFIG

STAMP = f"{NOW:%Y-%m-%dT%H:%M:%SZ}"


def put_json(storage, key, record):
    storage.put(key, json.dumps(record).encode())


def store_call(storage, market_id, prompt_id, verdict, ask, strategy="bond", cost=0.0001):
    snapshot = {"question": "Will X?", "strategy": strategy, "side": "NO", "values": {"ask": ask}}
    record = {
        "market_id": market_id,
        "prompt_id": prompt_id,
        "model": "deepseek-flash",
        "config_version": "v1",
        "user": json.dumps(snapshot),
        "cost_usd": cost,
        "verdict": {"verdict": verdict, "risk_flags": [], "confidence": 3, "reason": "r"},
    }
    key = f"llm_calls/date={NOW:%Y-%m-%d}/market={market_id}/{prompt_id}/scan={STAMP}.json"
    put_json(storage, key, record)


def store_cooldown(storage, arm, market_id, price, strategy="bond"):
    key = f"state/cooldowns/{arm}/{strategy}/{market_id}/{STAMP}.json"
    put_json(storage, key, {"rejected_at": NOW.isoformat(), "price": price})


def legacy_store(tmp_path):
    """The old layout: one object per position, resolution, overdue day, cooldown, LLM call."""
    storage = LocalStorage(tmp_path / "old")
    settled, late, bought = make_position("m1"), make_position("m2"), make_position("m3")
    bought_llm = make_position("m3", arm="prompt_buy")
    for p in (settled, late, bought, bought_llm):
        storage.put(position_key(p.arm, p.strategy, p.market_id), p.to_json())
    storage.put(resolution_key("baseline", "bond", "m1"), make_resolution(settled).to_json())
    for day, days_overdue in ((NOW - timedelta(days=1), 1.0), (NOW, 2.0)):
        report = make_overdue(late, days_overdue=days_overdue)
        storage.put(f"arms/baseline/bond/overdue/m2/{day:%Y-%m-%d}.json", report.to_json())
    store_call(storage, "m3", "buy_v1", "buy", ask=0.93)
    store_call(storage, "m3", "reject_v1", "reject", ask=0.93)
    for arm in ("prompt_reject", "mix_and"):
        store_cooldown(storage, arm, "m3", 0.93)
    store_call(storage, "m4", "buy_v1", "reject", ask=0.91)
    for arm in ("prompt_reject", "prompt_buy", "mix_and", "mix_or"):
        store_cooldown(storage, arm, "m4", 0.92)  # a retry: rejected at a newer ask
    return storage


def test_migration_puts_every_trade_in_one_table_with_its_status(tmp_path):
    source, dest = legacy_store(tmp_path), LocalStorage(tmp_path / "new")

    migrate(source, dest, "buy_v1", "reject_v1", workers=2, log=lambda _: None)

    ledger = load_ledger(dest)
    statuses = {(a, m): t.status for (a, _, m), t in ledger.trades.items()}
    assert statuses == {
        ("baseline", "m1"): "resolved",
        ("baseline", "m2"): "overdue",
        ("baseline", "m3"): "open",
        ("prompt_buy", "m3"): "open",
    }
    (overdue,) = ledger.overdue("baseline", "bond")
    assert overdue.days_overdue == 2.0  # the latest day's analysis


def test_migration_turns_llm_calls_and_cooldowns_into_decisions(tmp_path):
    source, dest = legacy_store(tmp_path), LocalStorage(tmp_path / "new")

    migrate(source, dest, "buy_v1", "reject_v1", workers=2, log=lambda _: None)

    by_market = {d.market_id: d for d in load_decisions(dest).decisions}
    m3, m4 = by_market["m3"], by_market["m4"]
    assert (m3.buy.verdict.verdict, m3.reject.verdict.verdict) == ("buy", "reject")
    assert m3.buy.cost_usd == 0.0001
    assert m3.outcomes == {
        "prompt_reject": "rejected",
        "prompt_buy": "opened",
        "mix_and": "rejected",
    }
    assert m4.reject.verdict.verdict == "skipped"
    assert m4.ask == 0.92  # the cooldown's price wins over the first attempt's snapshot


def test_migrated_cooldowns_match_the_old_records(tmp_path):
    source, dest = legacy_store(tmp_path), LocalStorage(tmp_path / "new")

    migrate(source, dest, "buy_v1", "reject_v1", workers=2, log=lambda _: None)

    cooldowns = Cooldowns(load_decisions(dest).decisions)
    assert cooldowns.active(CONFIG, "mix_or", "bond", "m4", 0.94)  # 2c from 0.92
    assert not cooldowns.active(CONFIG, "mix_or", "bond", "m4", 0.95)
    assert not cooldowns.active(CONFIG, "prompt_buy", "bond", "m3", 0.93)  # it opened


def test_migration_refuses_to_overwrite_existing_tables(tmp_path):
    source, dest = legacy_store(tmp_path), LocalStorage(tmp_path / "new")
    migrate(source, dest, "buy_v1", "reject_v1", workers=2, log=lambda _: None)
    before = dest.get(TRADES_KEY)

    with pytest.raises(MigrationError):
        migrate(source, dest, "buy_v1", "reject_v1", workers=2, log=lambda _: None)
    assert dest.get(TRADES_KEY) == before


def test_a_resolution_without_its_position_stops_the_migration(tmp_path):
    source = legacy_store(tmp_path)
    orphan = make_resolution(make_position("ghost"))
    source.put(resolution_key("baseline", "bond", "ghost"), orphan.to_json())

    with pytest.raises(MigrationError, match="no position"):
        migrate(source, LocalStorage(tmp_path / "new"), "buy_v1", "reject_v1", workers=2)
