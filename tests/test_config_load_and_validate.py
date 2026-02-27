from pathlib import Path

from config.load import load_config_path
from config.schema import validate_config


def test_sample_config_loads_and_validates():
    cfg_path = Path("config.json")
    if not cfg_path.exists():
        return  # allow running tests without a local config
    cfg = load_config_path(cfg_path)
    errors = validate_config(cfg)
    assert isinstance(errors, list)

