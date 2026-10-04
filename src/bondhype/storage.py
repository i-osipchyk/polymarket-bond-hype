from pathlib import Path
from typing import Protocol

import boto3
from botocore.exceptions import ClientError


class KeyNotFound(KeyError):
    """No object is stored under the requested key."""


class ImmutableKey(Exception):
    """A key already holds different bytes; stored data is append-only."""


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...


class LocalStorage:
    def __init__(self, root: Path):
        self._root = Path(root)

    def put(self, key: str, data: bytes) -> None:
        path = self._root / key
        if path.is_file():
            if path.read_bytes() != data:
                raise ImmutableKey(key)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        try:
            return (self._root / key).read_bytes()
        except FileNotFoundError:
            raise KeyNotFound(key) from None

    def exists(self, key: str) -> bool:
        return (self._root / key).is_file()

    def list(self, prefix: str) -> list[str]:
        return sorted(
            path.relative_to(self._root).as_posix()
            for path in self._root.rglob("*")
            if path.is_file() and path.relative_to(self._root).as_posix().startswith(prefix)
        )


class S3Storage:
    """Same contract as LocalStorage, backed by an S3 bucket. Not covered by tests yet."""

    def __init__(self, bucket: str, client=None):
        self._bucket = bucket
        self._s3 = client or boto3.client("s3")

    def put(self, key: str, data: bytes) -> None:
        if self.exists(key):
            if self.get(key) != data:
                raise ImmutableKey(key)
            return
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                raise KeyNotFound(key) from None
            raise

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        for page in self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=prefix
        ):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(keys)
