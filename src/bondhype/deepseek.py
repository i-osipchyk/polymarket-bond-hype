"""DeepSeek chat-completions adapter (OpenAI-compatible). One call in, raw text out."""

import json
import urllib.error
import urllib.request

from bondhype.llm import Completion, LLMError, Usage

BASE_URL = "https://api.deepseek.com"


class DeepSeekClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 120):
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout

    def complete(self, *, model: str, system: str, user: str) -> Completion:
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
                payload = json.load(response)
            usage = payload.get("usage") or {}
            return Completion(
                text=payload["choices"][0]["message"]["content"],
                usage=Usage(
                    cache_hit_tokens=usage.get("prompt_cache_hit_tokens", 0),
                    cache_miss_tokens=usage.get("prompt_cache_miss_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                ),
            )
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
        ) as exc:
            raise LLMError(repr(exc)) from exc
