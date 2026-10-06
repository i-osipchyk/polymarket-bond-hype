import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from bondhype.storage import Storage


class LLMError(Exception):
    """The provider call failed."""


class LLMClient(Protocol):
    def complete(self, *, model: str, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class Prompt:
    id: str
    system: str


def load_prompt(directory: Path, prompt_id: str) -> Prompt:
    return Prompt(id=prompt_id, system=(directory / f"{prompt_id}.txt").read_text())


@dataclass(frozen=True)
class LLMSetup:
    client: "LLMClient"
    reject: Prompt
    buy: Prompt


@dataclass(frozen=True)
class Verdict:
    verdict: str
    risk_flags: tuple[str, ...]
    confidence: int | None
    reason: str


MAX_ATTEMPTS = 2


RISK_FLAGS = (
    "ambiguous_resolution",
    "scheduled_catalyst",
    "dispute_risk",
    "thin_book",
    "insider_risk",
    "already_decided",
)
RiskFlag = Literal[*RISK_FLAGS]


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["buy", "reject"]
    risk_flags: list[RiskFlag]
    confidence: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1)


def _parse(reply: str) -> Verdict:
    parsed = _Reply.model_validate_json(reply)
    return Verdict(
        verdict=parsed.verdict,
        risk_flags=tuple(parsed.risk_flags),
        confidence=parsed.confidence,
        reason=parsed.reason,
    )


def _key(prompt: Prompt, market_id: str, now: datetime) -> str:
    return (
        f"llm_calls/date={now:%Y-%m-%d}/market={market_id}/{prompt.id}/"
        f"scan={now:%Y-%m-%dT%H:%M:%SZ}.json"
    )


def review(
    snapshot: dict,
    prompt: Prompt,
    client: LLMClient,
    *,
    model: str,
    storage: Storage,
    market_id: str,
    config_version: str,
    now: datetime,
) -> Verdict:
    """Ask the model once per (market, prompt, scan); a retried scan reuses the stored verdict."""
    key = _key(prompt, market_id, now)
    if storage.exists(key):
        return Verdict(**_stored_verdict(json.loads(storage.get(key))["verdict"]))
    user = json.dumps(snapshot, sort_keys=True)
    attempts = []
    verdict = None
    for _ in range(MAX_ATTEMPTS):
        try:
            reply = client.complete(model=model, system=prompt.system, user=user)
        except LLMError as exc:
            attempts.append({"output": None, "error": str(exc)})
            continue
        try:
            verdict = _parse(reply)
        except ValueError as exc:
            attempts.append({"output": reply, "error": str(exc)})
            continue
        attempts.append({"output": reply, "error": None})
        break
    if verdict is None:
        verdict = Verdict(verdict="error", risk_flags=(), confidence=None, reason="no valid reply")
    record = {
        "market_id": market_id,
        "prompt_id": prompt.id,
        "model": model,
        "config_version": config_version,
        "system": prompt.system,
        "user": user,
        "attempts": attempts,
        "verdict": asdict(verdict),
    }
    storage.put(key, json.dumps(record, sort_keys=True).encode())
    return verdict


def _stored_verdict(raw: dict) -> dict:
    return raw | {"risk_flags": tuple(raw["risk_flags"])}
