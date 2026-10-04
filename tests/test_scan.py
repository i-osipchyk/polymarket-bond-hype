import json
from pathlib import Path

import pytest

from bondhype.books import BookFetchError
from bondhype.config import load_config
from bondhype.scan import scan
from bondhype.storage import LocalStorage
from builders import NOW

CONFIG = load_config(Path(__file__).parent / "fixtures" / "config_valid.yaml")
LIVE = Path(__file__).parent / "fixtures" / "live"


def load(name: str) -> dict:
    return json.loads((LIVE / f"{name}.json").read_text())


def book_source(books_by_token: dict[str, dict]):
    return lambda token_id: books_by_token[token_id]


def read_records(storage) -> list[dict]:
    (key,) = storage.list("candidates/date=2026-10-04/")
    return [json.loads(line) for line in storage.get(key).decode().splitlines()]


YES_SIDE_BOOK = {
    "bids": [{"price": "0.07", "size": "100"}],
    "asks": [{"price": "0.08", "size": "100"}],
}


def test_scan_logs_a_passing_market_with_selection_raw_values_and_config_version(tmp_path):
    fixture = load("bond_sports_tight")
    market = fixture["gamma_market"]
    yes_token, no_token = json.loads(market["clobTokenIds"])
    storage = LocalStorage(tmp_path)

    scan(
        [market],
        book_source({yes_token: YES_SIDE_BOOK, no_token: fixture["clob_book"]}),
        storage,
        CONFIG,
        NOW,
    )

    (record,) = read_records(storage)
    assert record["market_id"] == "5102083"
    assert record["config_version"] == "2026-10-04.1"
    assert record["selected"] == {"strategy": "bond", "side": "NO"}
    bond_no = next(a for a in record["attempts"] if (a["strategy"], a["side"]) == ("bond", "NO"))
    assert bond_no["passed"] is True
    assert bond_no["values"]["ask"] == 0.93
    assert bond_no["values"]["spread"] == pytest.approx(0.01)


def test_scan_logs_rejected_markets_with_reasons(tmp_path):
    fixture = load("wide_spread_edge")
    market = fixture["gamma_market"]
    yes_token, no_token = json.loads(market["clobTokenIds"])
    storage = LocalStorage(tmp_path)

    scan(
        [market],
        book_source({yes_token: YES_SIDE_BOOK, no_token: fixture["clob_book"]}),
        storage,
        CONFIG,
        NOW,
    )

    (record,) = read_records(storage)
    assert record["selected"] is None
    hype = next(a for a in record["attempts"] if a["strategy"] == "hype")
    assert hype["passed"] is False
    assert sorted(hype["reasons"]) == ["insufficient_liquidity", "spread_too_wide"]
    assert hype["values"]["spread"] == pytest.approx(0.29)


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def test_candidate_file_is_strict_json_even_for_an_already_ended_market(tmp_path):
    fixture = load("bond_sports_tight")
    market = {**fixture["gamma_market"], "endDate": "2026-10-03T00:00:00Z"}
    yes_token, no_token = json.loads(market["clobTokenIds"])
    storage = LocalStorage(tmp_path)

    scan(
        [market],
        book_source({yes_token: YES_SIDE_BOOK, no_token: fixture["clob_book"]}),
        storage,
        CONFIG,
        NOW,
    )

    (key,) = storage.list("candidates/date=2026-10-04/")
    (line,) = storage.get(key).decode().splitlines()
    record = json.loads(line, parse_constant=_reject_constant)
    bond_no = next(a for a in record["attempts"] if (a["strategy"], a["side"]) == ("bond", "NO"))
    assert "days_out_of_range" in bond_no["reasons"]
    assert bond_no["values"]["annualised_yield"] is None


def test_unparseable_market_is_logged_as_rejected_and_does_not_abort_the_scan(tmp_path):
    fixture = load("bond_sports_tight")
    good = fixture["gamma_market"]
    broken = {k: v for k, v in good.items() if k != "endDate"} | {"id": "broken-1"}
    yes_token, no_token = json.loads(good["clobTokenIds"])
    storage = LocalStorage(tmp_path)

    scan(
        [broken, good],
        book_source({yes_token: YES_SIDE_BOOK, no_token: fixture["clob_book"]}),
        storage,
        CONFIG,
        NOW,
    )

    by_id = {r["market_id"]: r for r in read_records(storage)}
    assert set(by_id) == {"broken-1", "5102083"}
    assert by_id["broken-1"]["selected"] is None
    assert "endDate" in by_id["broken-1"]["parse_error"]
    assert by_id["5102083"]["selected"] == {"strategy": "bond", "side": "NO"}


def test_book_fetch_failure_fails_closed_for_that_side_only(tmp_path):
    fixture = load("bond_sports_tight")
    market = fixture["gamma_market"]
    yes_token, no_token = json.loads(market["clobTokenIds"])
    storage = LocalStorage(tmp_path)

    def fetch_book(token_id):
        if token_id == no_token:
            raise BookFetchError("HTTP 503")
        return YES_SIDE_BOOK

    scan([market], fetch_book, storage, CONFIG, NOW)

    (record,) = read_records(storage)
    assert record["selected"] is None
    reasons = {(a["strategy"], a["side"]): a["reasons"] for a in record["attempts"]}
    assert reasons[("bond", "NO")] == ["missing_book_data"]
    assert reasons[("hype", "NO")] == ["missing_book_data"]
    assert "missing_book_data" not in reasons[("bond", "YES")]


def test_malformed_book_is_treated_as_missing_data(tmp_path):
    fixture = load("bond_sports_tight")
    market = fixture["gamma_market"]
    storage = LocalStorage(tmp_path)

    scan([market], lambda token_id: {"bids": []}, storage, CONFIG, NOW)

    (record,) = read_records(storage)
    assert record["selected"] is None
    assert all("missing_book_data" in a["reasons"] for a in record["attempts"])


def test_retrying_a_scan_overwrites_nothing_and_a_later_scan_adds_a_file(tmp_path):
    from datetime import timedelta

    fixture = load("bond_sports_tight")
    market = fixture["gamma_market"]
    yes_token, no_token = json.loads(market["clobTokenIds"])
    books = book_source({yes_token: YES_SIDE_BOOK, no_token: fixture["clob_book"]})
    storage = LocalStorage(tmp_path)

    scan([market], books, storage, CONFIG, NOW)
    scan([market], books, storage, CONFIG, NOW)
    assert len(storage.list("candidates/date=2026-10-04/")) == 1

    scan([market], books, storage, CONFIG, NOW + timedelta(minutes=15))
    assert len(storage.list("candidates/date=2026-10-04/")) == 2


def test_market_far_from_every_price_band_is_prescreened_without_fetching_books(tmp_path):
    fixture = load("bond_sports_tight")
    market = {**fixture["gamma_market"], "outcomePrices": '["0.50", "0.50"]'}
    storage = LocalStorage(tmp_path)

    def fetch_book(token_id):
        raise AssertionError("books must not be fetched for a prescreened market")

    scan([market], fetch_book, storage, CONFIG, NOW)

    (record,) = read_records(storage)
    assert record["market_id"] == "5102083"
    assert record["selected"] is None
    assert record["prescreened"] == "no_price_in_any_band"


def test_prescreen_margin_keeps_a_market_whose_ask_could_land_inside_the_band(tmp_path):
    # Gamma shows NO at 0.88; the ask is typically ~1c above, so it must still be fetched
    fixture = load("bond_sports_tight")
    market = {**fixture["gamma_market"], "outcomePrices": '["0.12", "0.88"]'}
    storage = LocalStorage(tmp_path)
    fetched = []

    def fetch_book(token_id):
        fetched.append(token_id)
        return YES_SIDE_BOOK

    scan([market], fetch_book, storage, CONFIG, NOW)
    assert len(fetched) == 2
