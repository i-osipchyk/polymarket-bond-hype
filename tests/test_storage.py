import pytest

from bondhype.storage import ImmutableKey, KeyNotFound, LocalStorage


@pytest.fixture(params=["local"])
def storage(request, tmp_path):
    return LocalStorage(tmp_path)


def test_put_then_get_round_trips_bytes(storage):
    storage.put("candidates/date=2026-10-04/scan-0900.json", b'{"market_id": "m1"}')
    assert storage.get("candidates/date=2026-10-04/scan-0900.json") == b'{"market_id": "m1"}'


def test_get_missing_key_raises_key_not_found(storage):
    with pytest.raises(KeyNotFound):
        storage.get("books/date=2026-10-04/absent.json")


def test_exists_reflects_whether_key_was_written(storage):
    key = "books/date=2026-10-04/m1.json"
    assert not storage.exists(key)
    storage.put(key, b"{}")
    assert storage.exists(key)


def test_list_returns_sorted_keys_under_prefix_only(storage):
    storage.put("candidates/date=2026-10-04/b.json", b"1")
    storage.put("candidates/date=2026-10-04/a.json", b"2")
    storage.put("candidates/date=2026-10-05/c.json", b"3")
    storage.put("books/date=2026-10-04/d.json", b"4")

    assert storage.list("candidates/date=2026-10-04/") == [
        "candidates/date=2026-10-04/a.json",
        "candidates/date=2026-10-04/b.json",
    ]
    assert storage.list("nothing/here/") == []


def test_retry_with_identical_bytes_is_idempotent(storage):
    key = "arms/baseline/bond/positions/m1.json"
    storage.put(key, b'{"shares": 10}')
    storage.put(key, b'{"shares": 10}')
    assert storage.get(key) == b'{"shares": 10}'


def test_overwriting_a_key_with_different_bytes_is_refused(storage):
    key = "arms/baseline/bond/positions/m1.json"
    storage.put(key, b'{"shares": 10}')
    with pytest.raises(ImmutableKey):
        storage.put(key, b'{"shares": 11}')
    assert storage.get(key) == b'{"shares": 10}'
