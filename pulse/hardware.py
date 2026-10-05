"""Hardware profiles: the parts of the power model that belong to one machine.

Live readings (CPU package energy, GPU board power, fan RPM, SSD activity) are
measured. Everything without a watt sensor is an explicit, labelled assumption
kept in a profile file, so another workstation can describe its own parts.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path


# Wide ranges that fit most single-socket desktop workstations. They make the
# outlet estimate deliberately broad until a machine-specific profile is used.
GENERIC_PROFILE = {
    "name": "generic",
    "description": "Generic desktop assumptions; copy a profile from pulse/profiles/ and edit it.",
    "psu": {
        "label": "Unspecified 80 PLUS Gold PSU",
        "source": "Typical 80 PLUS Gold curve at 115 V (not a measured unit)",
        # (DC output watts, AC-to-DC efficiency)
        "efficiency_points": [[40, 0.75], [100, 0.85], [200, 0.88], [500, 0.90], [1000, 0.87]],
        "margin": 0.04,
    },
    "fixed_components": [
        {"label": "Motherboard + built-in I/O", "low_w": 15.0, "high_w": 45.0,
         "detail": "Chipset, board logic, networking and USB controllers"},
        {"label": "Memory", "low_w": 3.0, "high_w": 12.0,
         "detail": "Module estimate; RAM used does not imply RAM watts"},
        {"label": "USB devices + lighting", "low_w": 1.0, "high_w": 12.0,
         "detail": "Connected peripherals and LEDs; draw unmeasured"},
    ],
    "cpu_vrm_loss_fraction": [0.05, 0.12],
    "ssd": {"label": "NVMe SSD", "device": "nvme0n1"},
    # Fan groups share one control signal: {"label", "channels": [1, 3], "count": 6}
    "fan_groups": [],
    "board_fan_chip": None,
    "board_fan_channels": [],
    "fan_channel_labels": {},
    # GPU display names, keyed by UUID or by index ("0", "1", ...).
    "gpu_labels": {},
    "rapl_domain": "/sys/class/powercap/intel-rapl:0",
}


def default_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / "system-pulse" / "profile.json"


def load_profile(path: Path | str | None = None) -> dict:
    """Merge a profile file over the generic defaults; a missing file means generic."""
    profile = copy.deepcopy(GENERIC_PROFILE)
    path = Path(path) if path else default_path()
    if not path.is_file():
        return profile
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Hardware profile must be a JSON object: {path}")
    unknown = set(value) - set(GENERIC_PROFILE) - {"$comment"}
    if unknown:
        raise ValueError(f"Unknown hardware profile keys in {path}: {', '.join(sorted(unknown))}")
    for key, item in value.items():
        if key == "psu" and isinstance(item, dict):
            profile["psu"].update(item)
        elif key != "$comment":
            profile[key] = item
    points = profile["psu"]["efficiency_points"]
    if len(points) < 2 or any(not 0 < eff < 1 for _, eff in points) or \
            [w for w, _ in points] != sorted(w for w, _ in points):
        raise ValueError("PSU efficiency points need increasing watts and efficiencies between 0 and 1")
    return profile


def gpu_label(profile: dict, gpu: dict) -> str:
    labels = profile.get("gpu_labels") or {}
    return labels.get(gpu.get("uuid")) or labels.get(str(gpu.get("index"))) or f"GPU {gpu.get('index')}"
