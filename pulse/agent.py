"""System Pulse host agent: sample this workstation every few seconds.

Every sample stays on the machine: minute history and temperature alerts
(history.sqlite3), per-job energy (energy.sqlite3), and the latest snapshot
(latest.json), which the lab notebook reads. Uploading each snapshot to a
hosted dashboard is optional.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.request

from collector import Collector
from hardware import load_profile
from meter import CgroupLocator, EnergyMeter, readings_from
from monitoring import Monitor


CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
DEFAULT_CONFIG = CONFIG_HOME / "system-pulse/agent.json"
LATEST = STATE_HOME / "system-pulse/latest.json"
KNOWN_KEYS = {"site_url", "ingest_token", "headers", "sites_bypass_token", "profile",
              "allow_sudo", "experiments_dir", "interval_seconds", "alerts"}


def load_config(path: Path) -> dict:
    """A missing file means local-only recording with the generic profile."""
    if not path.is_file():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    unknown = set(value) - KNOWN_KEYS
    if unknown:
        raise ValueError(f"Unknown keys in {path}: {', '.join(sorted(unknown))}")
    if value.get("site_url") and not value.get("ingest_token"):
        raise ValueError("An upload site_url needs an ingest_token")
    return value


def upload_headers(config: dict) -> dict:
    headers = {"Content-Type": "application/json", "X-Ingest-Token": config["ingest_token"],
               "User-Agent": "system-pulse-agent/2"}
    headers.update(config.get("headers") or {})
    if config.get("sites_bypass_token"):  # hosted ChatGPT Sites deployments
        headers["OAI-Sites-Authorization"] = "Bearer " + config["sites_bypass_token"]
    return headers


def send(snapshot: dict, config: dict, override_url: str | None = None) -> tuple[int, dict]:
    origin = (override_url or config["site_url"]).rstrip("/")
    if not origin.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
        raise ValueError("The upload destination must be HTTPS or a local preview")
    payload = json.dumps(snapshot, separators=(",", ":")).encode()
    request = urllib.request.Request(origin + "/api/ingest", payload, upload_headers(config), method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}


def write_latest(snapshot: dict, path: Path = LATEST) -> None:
    current = {key: value for key, value in snapshot.items() if key not in ("history_batch", "alert_events")}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".latest.", delete=False) as stream:
        json.dump(current, stream, separators=(",", ":"))
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--url", help="Upload to this destination instead of site_url (local preview)")
    parser.add_argument("--no-upload", action="store_true", help="Record locally only")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    config = load_config(args.config)
    profile = load_profile(config.get("profile"))
    collector = Collector(profile, config.get("experiments_dir"), bool(config.get("allow_sudo")))
    monitor = Monitor(profile=profile)
    if config.get("alerts"):
        monitor.apply_settings(config["alerts"])
    interval = float(config.get("interval_seconds", 5))
    # A gap is several missed samples, whatever the sampling interval.
    meter = EnergyMeter(max_gap_seconds=max(30.0, 3 * interval))
    locate = CgroupLocator()
    uploading = not args.no_upload and bool(args.url or config.get("site_url"))
    last_report = ""
    while True:
        started = time.monotonic()
        try:
            snapshot = collector.collect()
            monitor.observe(snapshot)
            try:
                reading = readings_from(snapshot, collector.energy, collector.last_gpuq_status, locate)
                snapshot["energy"] = meter.observe(reading)
                locate.forget((job.get("runtime") or {}).get("name") for job in
                              (collector.last_gpuq_status or {}).get("jobs", []))
            except Exception as error:  # telemetry continues without the meter
                snapshot["warnings"].append(f"Energy meter unavailable: {type(error).__name__}: {error}")
            write_latest(snapshot)
            if uploading:
                result, acknowledgment = send(snapshot, config, args.url)
                if acknowledgment.get("monitoring_acknowledged"):
                    monitor.acknowledge(snapshot)
                    monitor.apply_settings(acknowledgment.get("settings", {}))
            if last_report:
                print("Telemetry restored", flush=True)
            last_report = ""
            if args.once:
                target = "uploaded" if uploading else "recorded"
                print(f"Snapshot {target}: {snapshot['hostname']}, {len(snapshot['gpus'])} GPUs, "
                      f"warnings: {', '.join(snapshot['warnings']) or 'none'}", flush=True)
                return
        except (OSError, ValueError, urllib.error.URLError) as exc:
            summary = type(exc).__name__ + ": " + str(exc)
            if summary != last_report:
                print("Telemetry cycle failed:", summary[:240], file=sys.stderr, flush=True)
                last_report = summary
            if args.once:
                raise SystemExit(1)
        time.sleep(max(0.2, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
