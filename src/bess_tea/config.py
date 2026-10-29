"""Load assumptions from YAML into a flat parameter dict.

A flat dict (``{"capex.cost_multiplier": ...}`` -> ``{"cost_multiplier": ...}``)
keeps sensitivity and Monte Carlo overrides trivial: any scalar can be
replaced by name.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "assumptions.yaml"
_SECTIONS = ("project", "capex", "opex", "degradation", "revenue", "finance")


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def base_params(cfg: dict[str, Any]) -> dict[str, Any]:
    """Flatten the model sections of the config into one parameter dict."""
    params: dict[str, Any] = {}
    for section in _SECTIONS:
        for key, val in cfg[section].items():
            if key in params:
                raise ValueError(f"duplicate parameter name: {key}")
            params[key] = val
    params["nrel_mid_curve"] = {int(k): float(v) for k, v in params["nrel_mid_curve"].items()}
    return params
