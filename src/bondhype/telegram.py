"""Telegram Bot API adapter: one text message in, delivered to the configured chat."""

import json
import urllib.error
import urllib.request

API_URL = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000  # Telegram rejects messages over 4096


class TelegramError(Exception):
    """The message could not be delivered."""


def _chunks(text: str) -> list[str]:
    """Split on line boundaries so each message fits Telegram's size limit."""
    chunks, current = [], ""
    for line in text.splitlines():
        line = line[:MAX_MESSAGE_CHARS]
        if current and len(current) + len(line) + 1 > MAX_MESSAGE_CHARS:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    return [*chunks, current] if current else chunks


class TelegramClient:
    def __init__(self, token: str, chat_id: str, base_url: str = API_URL, timeout: float = 30):
        self._url = f"{base_url}/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout

    def send(self, text: str) -> None:
        for chunk in _chunks(text):
            request = urllib.request.Request(
                self._url,
                data=json.dumps({"chat_id": self._chat_id, "text": chunk}).encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    if not json.load(response).get("ok"):
                        raise TelegramError("Telegram rejected the message")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise TelegramError(type(exc).__name__) from exc  # the URL embeds the bot token
