"""Thin HTTP clients for the public Gamma and CLOB APIs. Raw JSON in, raw JSON out."""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime

from bondhype.books import BookFetchError

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HEADERS = {"User-Agent": "polymarket-bond-hype/0.1"}


def _get(url: str, params: dict) -> dict:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def list_markets(
    end_date_min: datetime, end_date_max: datetime, volume_num_min: float
) -> Iterator[dict]:
    """Active markets ending in the window, paged with Gamma's keyset cursor."""
    params = {
        "limit": 100,
        "active": "true",
        "closed": "false",
        "end_date_min": _iso(end_date_min),
        "end_date_max": _iso(end_date_max),
        "volume_num_min": volume_num_min,
    }
    while True:
        page = _get(f"{GAMMA}/markets/keyset", params)
        yield from page["markets"]
        if not page.get("next_cursor"):
            return
        params["after_cursor"] = page["next_cursor"]


def fetch_book(token_id: str) -> dict:
    try:
        return _get(f"{CLOB}/book", {"token_id": token_id})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise BookFetchError(f"{token_id}: {exc!r}") from exc


def price_history(token_id: str, interval: str = "1d", fidelity_minutes: int = 60) -> list[dict]:
    """Price path for a token as [{"t": unix_seconds, "p": price}, ...]."""
    data = _get(
        f"{CLOB}/prices-history",
        {"market": token_id, "interval": interval, "fidelity": fidelity_minutes},
    )
    return data["history"]


def fetch_market(market_id: str) -> dict:
    return _get(f"{GAMMA}/markets/{market_id}", {})
