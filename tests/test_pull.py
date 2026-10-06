from bondhype.pull import default_prefixes, pull
from bondhype.storage import LocalStorage


def _remote(tmp_path, files):
    remote = LocalStorage(tmp_path / "remote")  # stands in for S3Storage: same Storage contract
    for key, data in files.items():
        remote.put(key, data)
    return remote


def test_pull_copies_only_the_requested_prefixes(tmp_path):
    remote = _remote(
        tmp_path,
        {
            "arms/baseline/bond/positions/m1.json": b"p1",
            "arms/baseline/bond/resolutions/m1.json": b"r1",
            "candidates/date=2026-10-07/scan=x.jsonl": b"c1",
        },
    )
    local = LocalStorage(tmp_path / "local")

    summary = pull(remote, local, ["arms/baseline/bond/positions/", "candidates/"])

    assert local.list("") == [
        "arms/baseline/bond/positions/m1.json",
        "candidates/date=2026-10-07/scan=x.jsonl",
    ]
    assert local.get("arms/baseline/bond/positions/m1.json") == b"p1"
    assert (summary.copied, summary.skipped) == (2, 0)


class CountingSource:
    def __init__(self, inner):
        self._inner = inner
        self.downloads: list[str] = []

    def list(self, prefix):
        return self._inner.list(prefix)

    def get(self, key):
        self.downloads.append(key)
        return self._inner.get(key)


def test_a_second_pull_downloads_only_keys_it_does_not_have_yet(tmp_path):
    remote = _remote(tmp_path, {"candidates/a.jsonl": b"a", "candidates/b.jsonl": b"b"})
    source = CountingSource(remote)
    local = LocalStorage(tmp_path / "local")
    pull(source, local, ["candidates/"])
    remote.put("candidates/c.jsonl", b"c")
    source.downloads.clear()

    summary = pull(source, local, ["candidates/"])

    assert source.downloads == ["candidates/c.jsonl"]
    assert (summary.copied, summary.skipped) == (1, 2)
    assert local.list("candidates/") == [f"candidates/{n}.jsonl" for n in "abc"]


ALL_KINDS = {
    "markets/date=2026-10-07/m.json": b"1",
    "candidates/date=2026-10-07/scan=x.jsonl": b"1",
    "llm_calls/date=2026-10-07/market=m1/reject_v1/scan=x.json": b"1",
    "arms/mix_or/hype/positions/m1.json": b"1",
    "arms/mix_or/hype/resolutions/m1.json": b"1",
    "arms/mix_or/hype/overdue/m1/2026-10-07.json": b"1",
    "reports/date=2026-10-07/report.txt": b"1",
    "state/date=2026-10-07/cooldown.json": b"1",
    "books/date=2026-10-07/m1.json": b"1",
    "arms/mix_or/hype/pricepath/m1/2026-10-07T09.json": b"1",
}
HEAVY = {"books/date=2026-10-07/m1.json", "arms/mix_or/hype/pricepath/m1/2026-10-07T09.json"}


def test_default_pull_takes_everything_except_raw_books_and_price_paths(tmp_path):
    remote = _remote(tmp_path, ALL_KINDS)
    local = LocalStorage(tmp_path / "local")

    pull(remote, local)

    assert set(local.list("")) == set(ALL_KINDS) - HEAVY


def test_heavy_data_is_pulled_when_asked_for(tmp_path):
    remote = _remote(tmp_path, ALL_KINDS)
    local = LocalStorage(tmp_path / "local")

    pull(remote, local, default_prefixes(include_heavy=True))

    assert set(local.list("")) == set(ALL_KINDS)
