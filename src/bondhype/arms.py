from bondhype.llm import Verdict

ARMS = ("baseline", "prompt_reject", "prompt_buy", "mix_and", "mix_or")


def route(*, reject: Verdict, buy: Verdict) -> set[str]:
    """Arms that trade a rules-passing candidate. Only an explicit buy counts; error is a reject."""
    reject_buys = reject.verdict == "buy"
    buy_buys = buy.verdict == "buy"
    arms = {"baseline"}
    if reject_buys:
        arms.add("prompt_reject")
    if buy_buys:
        arms.add("prompt_buy")
    if reject_buys and buy_buys:
        arms.add("mix_and")
    if reject_buys or buy_buys:
        arms.add("mix_or")
    return arms
