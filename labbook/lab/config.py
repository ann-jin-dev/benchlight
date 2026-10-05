"""labbook settings: where records live, and how to price energy.

Prices and grid carbon intensity are never guessed. Cost and CO2 appear on a
receipt only when the owner sets them for their own utility and grid.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .sources import default_paths

DEFAULTS = {
    **default_paths(),
    "electricity": {
        "price_per_kwh": None,     # e.g. 0.32, from your utility bill
        "currency": "USD",
        "grid_gco2_per_kwh": None,  # e.g. from your grid operator's annual report
        "grid_source": "",
    },
    "public": {
        "title": "A one-person AI lab, live",
        "subtitle": "",
        # Real project names are private. Only projects listed here appear, under
        # their alias; every other project is shown as "private project".
        "project_aliases": {},
        "show_recent_receipts": 8,
        "repo_url": "",
    },
}


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return Path(os.environ.get("LABBOOK_CONFIG", base / "labbook" / "config.json"))


def load_config(path: Path | None = None) -> dict:
    config = copy.deepcopy(DEFAULTS)
    path = path or config_path()
    if path.is_file():
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError(f"{path} must contain a JSON object")
        for key, item in value.items():
            if key not in DEFAULTS:
                raise ValueError(f"Unknown labbook setting {key!r} in {path}")
            if isinstance(DEFAULTS[key], dict):
                config[key].update(item)
            else:
                config[key] = item
    return config
