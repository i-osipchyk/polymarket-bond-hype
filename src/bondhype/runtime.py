"""Lambda runtime wiring: settings from the environment, secrets from the parameter store."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from bondhype import clients
from bondhype.config import Config, load_config
from bondhype.deepseek import DeepSeekClient
from bondhype.llm import LLMSetup, load_prompt
from bondhype.storage import S3Storage, Storage
from bondhype.telegram import TelegramClient


class RuntimeSetupError(Exception):
    """A required setting is missing."""


@dataclass(frozen=True)
class Runtime:
    storage: Storage
    config: Config
    llm: LLMSetup | None
    send: Callable[[str], None]
    list_markets: Callable[[datetime], Iterable[dict]]
    fetch_market: Callable[[str], dict]
    fetch_book: Callable[[str], dict]


def _ssm_secret(name: str) -> str:
    import boto3

    return boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _market_lister(config: Config) -> Callable[[datetime], Iterable[dict]]:
    bond, hype = config.bond, config.hype

    def list_markets(now: datetime) -> Iterable[dict]:
        return clients.list_markets(
            end_date_min=now
            + timedelta(days=min(bond.days_to_resolution_min, hype.days_to_resolution_min)),
            end_date_max=now
            + timedelta(days=max(bond.days_to_resolution_max, hype.days_to_resolution_max)),
            volume_num_min=min(bond.min_total_volume_usd, hype.min_total_volume_usd),
        )

    return list_markets


def build_runtime(
    env: Mapping[str, str],
    read_secret: Callable[[str], str] = _ssm_secret,
    make_storage: Callable[[str], Storage] = S3Storage,
) -> Runtime:
    """Env names the bucket, config and prompt paths, and the parameter names holding secrets."""

    def setting(name: str) -> str:
        if not env.get(name):
            raise RuntimeSetupError(f"missing setting {name}")
        return env[name]

    names = {
        name: setting(name)
        for name in (
            "BONDHYPE_BUCKET",
            "BONDHYPE_CONFIG",
            "BONDHYPE_PROMPTS_DIR",
            "DEEPSEEK_API_KEY_PARAM",
            "TELEGRAM_BOT_TOKEN_PARAM",
            "TELEGRAM_CHAT_ID_PARAM",
        )
    }
    config = load_config(Path(names["BONDHYPE_CONFIG"]))
    prompts_dir = Path(names["BONDHYPE_PROMPTS_DIR"])
    return Runtime(
        storage=make_storage(names["BONDHYPE_BUCKET"]),
        config=config,
        llm=LLMSetup(
            client=DeepSeekClient(
                read_secret(names["DEEPSEEK_API_KEY_PARAM"]), timeout=config.llm.timeout_seconds
            ),
            reject=load_prompt(prompts_dir, config.llm.reject_prompt),
            buy=load_prompt(prompts_dir, config.llm.buy_prompt),
        ),
        send=TelegramClient(
            read_secret(names["TELEGRAM_BOT_TOKEN_PARAM"]),
            read_secret(names["TELEGRAM_CHAT_ID_PARAM"]),
        ).send,
        list_markets=_market_lister(config),
        fetch_market=clients.fetch_market,
        fetch_book=clients.fetch_book,
    )
