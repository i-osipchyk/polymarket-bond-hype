from bondhype.models import Book, Level


class ParseError(ValueError):
    """An API response does not have the shape we validated against."""


class BookFetchError(Exception):
    """A CLOB book could not be fetched."""


def _levels(raw_levels: list[dict]) -> tuple[Level, ...]:
    return tuple(Level(float(x["price"]), float(x["size"])) for x in raw_levels)


def parse_book(raw: dict) -> Book:
    try:
        return Book(bids=_levels(raw["bids"]), asks=_levels(raw["asks"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError(f"unexpected CLOB book shape: {exc!r}") from exc
