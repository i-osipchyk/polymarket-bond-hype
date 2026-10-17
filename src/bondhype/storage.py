import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import boto3
from botocore.exceptions import ClientError


class KeyNotFound(KeyError):
    """No object is stored under the requested key."""


class StaleWrite(Exception):
    """The object changed since it was read (or appeared since it was found missing)."""


@dataclass(frozen=True)
class Versioned:
    """An object's bytes and the version they were read at; both None if it does not exist."""

    data: bytes | None
    version: str | None


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def read_versioned(self, key: str) -> Versioned: ...
    def write_versioned(self, key: str, data: bytes, version: str | None) -> str: ...


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalStorage:
    def __init__(self, root: Path):
        self._root = Path(root)

    # Versioned calls use these rather than put/get, so each call is one request, as on S3.
    def _write(self, key: str, data: bytes) -> None:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _read(self, key: str) -> bytes | None:
        try:
            return (self._root / key).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, key: str, data: bytes) -> None:
        self._write(key, data)

    def get(self, key: str) -> bytes:
        data = self._read(key)
        if data is None:
            raise KeyNotFound(key)
        return data

    def exists(self, key: str) -> bool:
        return (self._root / key).is_file()

    def list(self, prefix: str) -> list[str]:
        return sorted(
            path.relative_to(self._root).as_posix()
            for path in self._root.rglob("*")
            if path.is_file() and path.relative_to(self._root).as_posix().startswith(prefix)
        )

    def read_versioned(self, key: str) -> Versioned:
        data = self._read(key)
        return Versioned(data, None if data is None else _digest(data))

    def write_versioned(self, key: str, data: bytes, version: str | None) -> str:
        current = self._read(key)
        if (None if current is None else _digest(current)) != version:
            raise StaleWrite(key)
        self._write(key, data)
        return _digest(data)


class S3Storage:
    """Same contract as LocalStorage, backed by an S3 bucket."""

    def __init__(self, bucket: str, client=None):
        self._bucket = bucket
        self._s3 = client or boto3.client("s3")

    def put(self, key: str, data: bytes) -> None:
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

    def read_versioned(self, key: str) -> Versioned:
        """One GetObject; the version is the ETag."""
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                return Versioned(None, None)
            raise
        return Versioned(response["Body"].read(), response["ETag"])

    def write_versioned(self, key: str, data: bytes, version: str | None) -> str:
        """One conditional PutObject: If-Match the version read, or If-None-Match for a new key."""
        condition = {"IfNoneMatch": "*"} if version is None else {"IfMatch": version}
        try:
            response = self._s3.put_object(Bucket=self._bucket, Key=key, Body=data, **condition)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in (
                "PreconditionFailed",
                "ConditionalRequestConflict",
            ):
                raise StaleWrite(key) from None
            raise
        return response["ETag"]
