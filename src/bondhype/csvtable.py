"""Plain CSV tables: one header row, every cell a string, empty for None.

Floats are written with `repr`, which round-trips exactly, so a table read back gives the same
numbers the stats were computed from. Tuples are JSON arrays.
"""

import csv
import io
import json
from collections.abc import Iterable, Sequence
from datetime import date, datetime


def encode(columns: Sequence[str], rows: Iterable[dict[str, str]]) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def decode(columns: Sequence[str], data: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(data.decode()))
    if reader.fieldnames != list(columns):
        raise ValueError(f"unexpected columns {reader.fieldnames}")
    return list(reader)


def text(value: str | None) -> str:
    return "" if value is None else value


def number(value: float | int | None) -> str:
    return "" if value is None else repr(value)


def flag(value: bool | None) -> str:
    return "" if value is None else ("true" if value else "false")


def moment(value: datetime | date | None) -> str:
    return "" if value is None else value.isoformat()


def items(value: Sequence[str] | None) -> str:
    return "" if value is None else json.dumps(list(value))


def opt_text(cell: str) -> str | None:
    return cell or None


def to_float(cell: str) -> float:
    return float(cell)


def opt_float(cell: str) -> float | None:
    return float(cell) if cell else None


def opt_int(cell: str) -> int | None:
    return int(cell) if cell else None


def to_bool(cell: str) -> bool:
    if cell not in ("true", "false"):
        raise ValueError(f"not a bool: {cell!r}")
    return cell == "true"


def to_datetime(cell: str) -> datetime:
    return datetime.fromisoformat(cell)


def to_items(cell: str) -> tuple[str, ...]:
    return tuple(json.loads(cell)) if cell else ()
