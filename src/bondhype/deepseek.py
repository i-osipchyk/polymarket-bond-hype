"""DeepSeek chat-completions adapter (OpenAI-compatible). One call in, raw text out."""

import json
import urllib.error
import urllib.request

from bondhype.llm import LLMError

BASE_URL = "https://api.deepseek.com"


class DeepSeekClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 120):
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout

    def complete(self, *, model: str, system: str, user: str) -> str:
        body = {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.load(response)["choices"][0]["message"]["content"]
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
        ) as exc:
            raise LLMError(repr(exc)) from exc
