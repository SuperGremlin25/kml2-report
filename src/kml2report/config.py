"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "field_map": {},
    "defaults": {},
    "filters": [],
    "description_separators": [";", "\n", "<br>", "<br/>", "<br />"],
    "output": {
        "format": "csv",
        "include_raw": True,
        "coordinate_format": "decimal",
    },
}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load a YAML config file and merge with defaults."""
    config = _deep_copy_dict(DEFAULT_CONFIG)
    if path is None:
        return config

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        user_config = yaml.safe_load(fh) or {}

    _merge(config, user_config)
    return config


def _merge(base: dict, override: dict) -> None:
    """Recursively merge override into base."""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _merge(base[key], val)
        else:
            base[key] = val


def _deep_copy_dict(d: dict) -> dict:
    """Simple deep copy for nested dicts/lists of primitives."""
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _deep_copy_dict(v)
        elif isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = v
    return out
