"""Load local environment variables and application YAML configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from selfheal.site_policy import blocked_resource_hosts


def load_environment(env_file: str | Path = ".env") -> bool:
    """Load project environment variables without replacing shell-provided values."""
    return load_dotenv(Path(env_file))


def load_app_config(config_path: str | Path, env: str = "prod") -> dict[str, Any]:
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

    execution = config.setdefault("execution", {})
    if not isinstance(execution, dict):
        raise ValueError(f"Execution settings must be a YAML mapping in {path}")
    blocked = execution.get("blocked_resource_host_suffixes") or []
    if not isinstance(blocked, list) or not all(isinstance(host, str) for host in blocked):
        raise ValueError(f"Blocked resource hosts must be a YAML list of names in {path}")
    hosts = blocked_resource_hosts(str(environment["base_url"]), blocked)
    if hosts:
        execution["blocked_resource_host_suffixes"] = hosts

    return config
