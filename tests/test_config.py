from pathlib import Path

import pytest
import yaml

from bondhype.config import ConfigError, load_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_valid_config_loads_with_its_version():
    config = load_config(FIXTURES / "config_valid.yaml")
    assert config.version == "2026-10-04.1"


def test_valid_config_exposes_filter_thresholds():
    config = load_config(FIXTURES / "config_valid.yaml")
    assert (config.bond.price_min, config.bond.price_max) == (0.90, 0.95)
    assert config.bond.max_spread == 0.02
    assert config.bond.min_annualised_yield == 2.0
    assert config.order_size_usd == 10
    assert config.prescreen_price_margin == 0.03
    assert config.portfolio.starting_balance_usd == 1000
    assert config.portfolio.max_positions_per_event == 5
    assert config.portfolio.max_deployed_fraction == 0.30
    assert (config.portfolio.cooldown_hours, config.portfolio.cooldown_price_move) == (24, 0.03)
    assert (config.hype.yes_price_min, config.hype.yes_price_max) == (0.05, 0.25)
    assert config.hype.max_spread == 0.03
    assert config.hype.days_to_resolution_max == 14


def _write_variant(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load((FIXTURES / "config_valid.yaml").read_text())
    mutate(raw)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: c.pop("version"), id="missing-version"),
        pytest.param(lambda c: c.update(surprise=1), id="unknown-key"),
        pytest.param(lambda c: c["bond"].update(max_spread="wide"), id="mistyped-value"),
        pytest.param(
            lambda c: c["bond"].update(price_min=0.96, price_max=0.90), id="inverted-price-band"
        ),
        pytest.param(lambda c: c["hype"].update(yes_price_max=1.5), id="price-above-one"),
    ],
)
def test_invalid_config_is_rejected_with_config_error(tmp_path, mutate):
    with pytest.raises(ConfigError):
        load_config(_write_variant(tmp_path, mutate))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")
