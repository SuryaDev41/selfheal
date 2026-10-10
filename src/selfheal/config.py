"""Load local environment variables and application YAML configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG = Path("config/apps/automationexercise_openai.yaml")


def load_environment(env_file: str | Path = ".env") -> bool:
    """Load project environment variables without replacing shell-provided values."""
    return load_dotenv(Path(env_file))


def load_app_config(config_path: str | Path = DEFAULT_CONFIG, env: str = "prod") -> dict[str, Any]:
    """Read and validate the configured browser-test environment."""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    if not isinstance(config, dict):
        raise ValueError(f"Config is not a YAML mapping: {path}")

    environments = config.get("environments")
    if not isinstance(environments, Mapping) or env not in environments:
        raise ValueError(f"Environment {env!r} is not configured in {path}")

    environment = environments[env]
    if not isinstance(environment, Mapping) or not environment.get("base_url"):
        raise ValueError(f"Environment {env!r} has no base_url in {path}")

    return config
