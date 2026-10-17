import boto3
import pytest
from moto import mock_aws

from bondhype.storage import KeyNotFound, LocalStorage, S3Storage, StaleWrite

BUCKET = "bondhype-test"


@pytest.fixture
def s3_client(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="eu-west-1")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"}
        )
        yield client


@pytest.fixture(params=["local", "s3"])
def storage(request, tmp_path):
    if request.param == "local":
        return LocalStorage(tmp_path)
    return S3Storage(BUCKET, client=request.getfixturevalue("s3_client"))


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


def test_put_overwrites_a_key(storage):
    key = "candidates/date=2026-10-04/scan=x.jsonl"
    storage.put(key, b"first attempt")
    storage.put(key, b"retry")
    assert storage.get(key) == b"retry"


def test_versioned_read_of_a_missing_key_has_no_data_and_no_version(storage):
    stored = storage.read_versioned("state/trades.csv")
    assert (stored.data, stored.version) == (None, None)


def test_versioned_write_creates_then_replaces_the_version_it_read(storage):
    key = "state/trades.csv"
    first = storage.write_versioned(key, b"v1", None)
    assert storage.read_versioned(key).version == first

    second = storage.write_versioned(key, b"v2", first)

    stored = storage.read_versioned(key)
    assert (stored.data, stored.version) == (b"v2", second)


def test_versioned_write_refuses_a_key_that_changed_since_it_was_read(storage):
    key = "state/trades.csv"
    read_at = storage.write_versioned(key, b"v1", None)
    storage.write_versioned(key, b"another job", read_at)

    with pytest.raises(StaleWrite):
        storage.write_versioned(key, b"lost update", read_at)
    assert storage.get(key) == b"another job"


def test_versioned_create_refuses_a_key_that_appeared_meanwhile(storage):
    key = "state/trades.csv"
    storage.write_versioned(key, b"another job", None)

    with pytest.raises(StaleWrite):
        storage.write_versioned(key, b"lost update", None)
    assert storage.get(key) == b"another job"


def test_list_pages_past_a_thousand_keys(storage):
    for i in range(1001):
        storage.put(f"state/k/{i:04d}.json", b"{}")
    keys = storage.list("state/k/")
    assert len(keys) == 1001
    assert keys[0] == "state/k/0000.json" and keys[-1] == "state/k/1000.json"


def requests_made(client) -> list[str]:
    calls: list[str] = []
    client.meta.events.register("before-call.s3", lambda model, **_: calls.append(model.name))
    return calls


def test_s3_put_is_one_request(s3_client):
    storage = S3Storage(BUCKET, client=s3_client)
    calls = requests_made(s3_client)
    storage.put("candidates/date=2026-10-04/scan=x.jsonl", b"{}")
    assert calls == ["PutObject"]


def test_s3_versioned_read_and_write_are_one_request_each(s3_client):
    storage = S3Storage(BUCKET, client=s3_client)
    storage.write_versioned("state/trades.csv", b"v1", None)
    calls = requests_made(s3_client)

    stored = storage.read_versioned("state/trades.csv")
    storage.write_versioned("state/trades.csv", b"v2", stored.version)

    assert calls == ["GetObject", "PutObject"]
