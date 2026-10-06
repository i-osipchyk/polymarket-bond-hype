import pytest

from bondhype.arms import route
from bondhype.llm import Verdict


def v(kind: str) -> Verdict:
    confidence = None if kind == "error" else 3
    return Verdict(verdict=kind, risk_flags=(), confidence=confidence, reason="r")


@pytest.mark.parametrize(
    ("reject_prompt", "buy_prompt", "arms"),
    [
        ("buy", "buy", {"baseline", "prompt_reject", "prompt_buy", "mix_and", "mix_or"}),
        ("buy", "reject", {"baseline", "prompt_reject", "mix_or"}),
        ("reject", "buy", {"baseline", "prompt_buy", "mix_or"}),
        ("reject", "reject", {"baseline"}),
        ("error", "buy", {"baseline", "prompt_buy", "mix_or"}),
        ("buy", "error", {"baseline", "prompt_reject", "mix_or"}),
        ("error", "error", {"baseline"}),
    ],
)
def test_arms_that_trade_follow_the_readme_table(reject_prompt, buy_prompt, arms):
    assert route(reject=v(reject_prompt), buy=v(buy_prompt)) == arms
