from collections import Counter

from bondhype.storage import LocalStorage


class CountingStorage(LocalStorage):
    """LocalStorage that counts calls, one per S3 request the real store would make."""

    def __init__(self, root):
        super().__init__(root)
        self.calls: Counter[str] = Counter()
        self.got: list[str] = []
        self.listed: list[str] = []

    def reset(self) -> None:
        self.calls.clear()
        self.got.clear()
        self.listed.clear()

    def put(self, key, data):
        self.calls["put"] += 1
        super().put(key, data)

    def get(self, key):
        self.calls["get"] += 1
        self.got.append(key)
        return super().get(key)

    def exists(self, key):
        self.calls["exists"] += 1
        return super().exists(key)

    def list(self, prefix):
        self.calls["list"] += 1
        self.listed.append(prefix)
        return super().list(prefix)

    def read_versioned(self, key):
        self.calls["read_versioned"] += 1
        self.got.append(key)
        return super().read_versioned(key)

    def write_versioned(self, key, data, version):
        self.calls["write_versioned"] += 1
        return super().write_versioned(key, data, version)
