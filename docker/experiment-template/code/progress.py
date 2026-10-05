"""Write training progress for the private System Pulse dashboard.

Call report_progress(...) in a training loop running in this Docker environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time


def report_progress(
    step: int,
    total_steps: int,
    *,
    status: str = "running",
    metric_name: str = "",
    metric_value: float | None = None,
    epoch: int | None = None,
    total_epochs: int | None = None,
    message: str = "",
    path: str | None = None,
) -> None:
    destination = Path(path or os.environ.get("DASHBOARD_PROGRESS_PATH", "/workspace/outputs/progress.json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "step": step,
        "total_steps": total_steps,
        "status": status,
        "metric_name": metric_name,
        "metric_value": metric_value,
        "epoch": epoch,
        "total_epochs": total_epochs,
        "message": message,
        "updated_at": time.time(),
    }
    with tempfile.NamedTemporaryFile("w", dir=destination.parent, prefix=".progress-", delete=False) as handle:
        json.dump(record, handle, separators=(",", ":"))
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, destination)
