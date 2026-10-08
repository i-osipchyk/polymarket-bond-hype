from pathlib import Path

import pytest

from bondhype.runtime import RuntimeSetupError, build_runtime
from bondhype.storage import LocalStorage

ROOT = Path(__file__).parent.parent
ENV = {
    "BONDHYPE_BUCKET": "forward-test-bucket",
    "BONDHYPE_CONFIG": str(ROOT / "config" / "config.yaml"),
    "BONDHYPE_PROMPTS_DIR": str(ROOT / "prompts"),
    "BONDHYPE_PRICING": str(ROOT / "deepseek_pricing.yaml"),
    "DEEPSEEK_API_KEY_PARAM": "/bondhype/deepseek",
    "TELEGRAM_BOT_TOKEN_PARAM": "/bondhype/telegram-token",
    "TELEGRAM_CHAT_ID_PARAM": "/bondhype/telegram-chat",
}
SECRETS = {
    "/bondhype/deepseek": "dk",
    "/bondhype/telegram-token": "tok",
    "/bondhype/telegram-chat": "42",
}


def test_runtime_is_wired_from_env_with_secrets_read_from_the_parameter_store(tmp_path):
    buckets = []

    def make_storage(bucket):
        buckets.append(bucket)
        return LocalStorage(tmp_path)

    runtime = build_runtime(ENV, read_secret=SECRETS.__getitem__, make_storage=make_storage)

    assert buckets == ["forward-test-bucket"]
    assert runtime.config.version == "v4"
    assert (runtime.llm.reject.id, runtime.llm.buy.id) == ("reject_v1", "buy_v1")
    assert callable(runtime.send)


@pytest.mark.parametrize("missing", sorted(ENV))
def test_runtime_fails_fast_naming_the_missing_setting(missing):
    env = {k: v for k, v in ENV.items() if k != missing}

    with pytest.raises(RuntimeSetupError, match=missing):
        build_runtime(env, read_secret=SECRETS.__getitem__, make_storage=lambda b: None)
