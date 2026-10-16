import boto3
import pytest
from moto import mock_aws

from bondhype.storage import ImmutableKey, KeyNotFound, LocalStorage, S3Storage

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


def test_s3_writes_a_new_key_in_one_request(s3_client):
    storage = S3Storage(BUCKET, client=s3_client)
    calls = requests_made(s3_client)
    storage.put("arms/baseline/bond/positions/m1.json", b'{"shares": 10}')
    assert calls == ["PutObject"]


def test_s3_conditional_write_still_refuses_different_bytes(s3_client):
    storage = S3Storage(BUCKET, client=s3_client)
    key = "arms/baseline/bond/positions/m1.json"
    storage.put(key, b'{"shares": 10}')
    with pytest.raises(ImmutableKey):
        storage.put(key, b'{"shares": 11}')
    storage.put(key, b'{"shares": 10}')
    assert storage.get(key) == b'{"shares": 10}'
