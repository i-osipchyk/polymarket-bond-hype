import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from bondhype.pricing import Pricing, UnknownModel, Usage, call_cost

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """The provider call failed."""


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage = Usage()


class LLMClient(Protocol):
    def complete(self, *, model: str, system: str, user: str) -> Completion: ...


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
    pricing: Pricing


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


@dataclass(frozen=True)
class ReviewResult:
    """The verdict, and the full call record (input, every attempt's output, usage and cost)."""

    verdict: Verdict
    record: dict

    @property
    def cost_usd(self) -> float | None:
        return self.record["cost_usd"]


def review(
    snapshot: dict,
    prompt: Prompt,
    client: LLMClient,
    *,
    model: str,
    pricing: Pricing,
    market_id: str,
    config_version: str,
    now: datetime,
) -> ReviewResult:
    """Ask the model, retrying once on a failed call or an invalid reply; then `error`."""
    started = time.monotonic()
    user = json.dumps(snapshot, sort_keys=True)
    attempts = []
    verdict = None
    for number in range(1, MAX_ATTEMPTS + 1):
        try:
            completion = client.complete(model=model, system=prompt.system, user=user)
        except LLMError as exc:
            attempts.append({"output": None, "error": str(exc), "usage": None, "cost_usd": None})
            logger.warning("llm %s %s attempt %d failed: %s", prompt.id, market_id, number, exc)
            continue
        reply = completion.text
        billing = _billing(completion.usage, model, pricing, now)
        try:
            verdict = _parse(reply)
        except ValueError as exc:
            attempts.append({"output": reply, "error": str(exc)} | billing)
            logger.warning("llm %s %s attempt %d failed: %s", prompt.id, market_id, number, exc)
            continue
        attempts.append({"output": reply, "error": None} | billing)
        break
    if verdict is None:
        verdict = Verdict(verdict="error", risk_flags=(), confidence=None, reason="no valid reply")
    record = {
        "market_id": market_id,
        "prompt_id": prompt.id,
        "model": model,
        "config_version": config_version,
        "scan_at": now.isoformat(),
        "system": prompt.system,
        "user": user,
        "attempts": attempts,
        "cost_usd": _total_cost(attempts),
        "verdict": asdict(verdict),
    }
    logger.info(
        "llm %s %s -> %s in %.1fs",
        prompt.id,
        market_id,
        verdict.verdict,
        time.monotonic() - started,
    )
    return ReviewResult(verdict, record)


def _billing(usage: Usage, model: str, pricing: Pricing, now: datetime) -> dict:
    try:
        cost = call_cost(usage, model, now, pricing)
    except UnknownModel:
        logger.warning("no price for model %s; cost left blank", model)
        cost = None
    return {"usage": asdict(usage), "cost_usd": cost}


def _total_cost(attempts: list[dict]) -> float | None:
    """Failed calls cost nothing; if any billed attempt could not be priced the total is unknown."""
    billed = [a for a in attempts if a["usage"] is not None]
    if any(a["cost_usd"] is None for a in billed):
        return None
    return sum(a["cost_usd"] for a in billed)
