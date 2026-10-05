"""Durable minute history and temperature alerts for System Pulse."""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from hardware import GENERIC_PROFILE, gpu_label


RETENTION_MS = 12 * 60 * 60 * 1000
DEFAULT_SETTINGS = {
    "rules": {
        "cpu": {"warning": 85, "critical": 92, "limit": 95, "enabled": True},
        "gpu": {"warning": 78, "critical": 84, "limit": 88, "enabled": True},
        "ssd": {"warning": 60, "critical": 67, "limit": 70, "enabled": True},
    },
    "warning_hold_seconds": 30,
    "critical_hold_seconds": 10,
    "recovery_hold_seconds": 60,
    "hysteresis_c": 3,
}


def extract_metrics(snapshot: dict, profile: dict = GENERIC_PROFILE) -> tuple[dict, dict]:
    values, catalog = {}, {}

    def add(key, label, category, unit, value, family=None):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            return
        values[key] = round(value, 3)
        catalog[key] = {"id": key, "label": label, "category": category, "unit": unit}
        if family:
            catalog[key]["family"] = family

    add("cpu", "CPU", "usage", "%", snapshot.get("cpu", {}).get("total"))
    add("ram", "RAM", "usage", "%", snapshot.get("memory", {}).get("percent"))
    add("storage", "Storage", "usage", "%", snapshot.get("disk", {}).get("percent"))
    load = snapshot.get("load", [])
    add("load", "CPU load · 1 min", "load", "load", load[0] if load else None)
    for direction in ("down", "up"):
        add(f"network:{direction}", f"Network {'download' if direction == 'down' else 'upload'}",
            "network", "B/s", snapshot.get("network", {}).get(f"{direction}_bps"))
    for sensor in snapshot.get("sensors", {}).get("temperatures", []):
        name = sensor["name"]
        family = "cpu" if name == "CPU package" else "ssd" if name == "SSD" or name.startswith("SSD ") and "sensor" not in name.lower() else None
        add(f"temperature:{name}", name, "temperature", "°C", sensor.get("celsius"), family)
    for fan in snapshot.get("sensors", {}).get("fans", []):
        name = fan["name"]
        displayed = profile.get("board_fan_channels") or []
        if name.startswith("Board channel ") and displayed and \
                name not in {f"Board channel {i}" for i in displayed}:
            continue
        label = name
        if name.startswith("Board channel "):
            channel = name.rsplit(" ", 1)[-1]
            group = (profile.get("fan_channel_labels") or {}).get(channel)
            label = f"{group} · channel {channel}" if group else name
        add(f"fan:{name}", label, "fans", "RPM", fan.get("rpm"))
    for gpu in snapshot.get("gpus", []):
        key = "gpu:" + gpu["uuid"]
        label = gpu_label(profile, gpu)
        add(key + ":usage", label + " GPU", "usage", "%", gpu.get("utilization"))
        total = gpu.get("vram_total", 0)
        add(key + ":vram", label + " VRAM", "usage", "%", 100 * gpu.get("vram_used", 0) / total if total else None)
        add(key + ":temperature", label + " GPU", "temperature", "°C", gpu.get("temperature"), "gpu")
        add(key + ":power", label + " GPU", "power", "W", gpu.get("power_w"))
        for fan in gpu.get("fan_channels", []):
            add(key + f":fan:{fan['index']}", label + f" fan {fan['index']}", "fans", "RPM", fan.get("rpm"))
    power = snapshot.get("power", {})
    add("power:cpu", "CPU package", "power", "W", power.get("cpu_package_w"))
    if power.get("measured_ac_w") is not None:
        add("power:wall", "Measured outlet", "power", "W", power["measured_ac_w"])
    else:
        add("power:low", "Outlet estimate · low", "power", "W", power.get("estimate_low_w"))
        add("power:high", "Outlet estimate · high", "power", "W", power.get("estimate_high_w"))
    return values, catalog


def advance_alert(state: dict, value: float, rule: dict, settings: dict, now_ms: int) -> str | None:
    """Debounce transitions; clear only after sustained cooling with hysteresis."""
    if now_ms - state.get("last_seen", now_ms) > 20_000:
        state.pop("candidate", None)
        state.pop("candidate_since", None)
    state["last_seen"] = now_ms
    state["value"] = value
    current = state.get("level", "normal")
    target = None
    hold = 0
    if not rule.get("enabled", True):
        state["level"] = "normal"
    elif current != "critical" and value >= rule["critical"]:
        target, hold = "critical", settings["critical_hold_seconds"]
    elif current == "normal" and value >= rule["warning"]:
        target, hold = "warning", settings["warning_hold_seconds"]
    elif current != "normal" and value <= rule["warning"] - settings["hysteresis_c"]:
        target, hold = "recovery", settings["recovery_hold_seconds"]
    if target is None:
        state.pop("candidate", None)
        state.pop("candidate_since", None)
        return None
    if state.get("candidate") != target:
        state["candidate"] = target
        state["candidate_since"] = now_ms
    if now_ms - state["candidate_since"] < hold * 1000:
        return None
    state["level"] = "normal" if target == "recovery" else target
    state["started_at"] = now_ms
    state.pop("candidate", None)
    state.pop("candidate_since", None)
    return target


class Monitor:
    def __init__(self, path: Path | str | None = None, profile: dict = GENERIC_PROFILE):
        self.profile = profile
        if path is None:
            state_root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            path = state_root / "system-pulse/history.sqlite3"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS buckets (
                timestamp INTEGER PRIMARY KEY, totals TEXT NOT NULL,
                samples INTEGER NOT NULL, revision INTEGER NOT NULL,
                sent_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, timestamp INTEGER NOT NULL,
                payload TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.settings = self._read_state("settings", copy.deepcopy(DEFAULT_SETTINGS))
        self.catalog = self._read_state("catalog", {})
        self.alerts = self._read_state("alerts", {})
        self.last_cleanup = 0

    def _read_state(self, key, default):
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _save_state(self, key, value):
        self.db.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, separators=(",", ":"))))

    def apply_settings(self, settings: dict):
        if not isinstance(settings, dict) or not isinstance(settings.get("rules"), dict):
            return
        for family, default in DEFAULT_SETTINGS["rules"].items():
            rule = settings["rules"].get(family, {})
            warning, critical = rule.get("warning"), rule.get("critical")
            if not isinstance(warning, (int, float)) or not isinstance(critical, (int, float)):
                return
            if not 30 <= warning < critical <= default["limit"]:
                return
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        for family in self.settings["rules"]:
            self.settings["rules"][family].update({key: settings["rules"][family][key] for key in ("warning", "critical")})
            self.settings["rules"][family]["enabled"] = bool(settings["rules"][family].get("enabled", True))
        self._save_state("settings", self.settings)
        self.db.commit()

    def observe(self, snapshot: dict):
        now_ms = int(snapshot.get("timestamp", time.time()) * 1000)
        bucket_ms = now_ms // 60_000 * 60_000
        values, catalog = extract_metrics(snapshot, self.profile)
        self.catalog.update(catalog)
        row = self.db.execute("SELECT totals,samples FROM buckets WHERE timestamp=?", (bucket_ms,)).fetchone()
        totals, samples = (json.loads(row[0]), row[1]) if row else ({}, 0)
        for key, value in values.items():
            previous = totals.get(key)
            totals[key] = [previous[0] + value, previous[1] + 1, min(previous[2], value), max(previous[3], value), value] if previous else [value, 1, value, value, value]
        self.db.execute("INSERT INTO buckets(timestamp,totals,samples,revision) VALUES(?,?,?,?) ON CONFLICT(timestamp) DO UPDATE SET totals=excluded.totals,samples=excluded.samples,revision=excluded.revision",
                        (bucket_ms, json.dumps(totals, separators=(",", ":")), samples + 1, now_ms))
        for key, value in values.items():
            descriptor = catalog[key]
            family = descriptor.get("family")
            if family not in self.settings["rules"]:
                continue
            rule = self.settings["rules"][family]
            state = self.alerts.setdefault(key, {"level": "normal"})
            transition = advance_alert(state, value, rule, self.settings, now_ms)
            if transition:
                threshold = rule["critical"] if transition == "critical" else rule["warning"]
                message = f"{descriptor['label']} {'cooled to' if transition == 'recovery' else 'reached'} {value:.1f}°C"
                if transition != "recovery":
                    message += f"; {transition} threshold {threshold:g}°C"
                event = {"id": str(uuid.uuid4()), "timestamp": now_ms, "metric_id": key,
                         "sensor": descriptor["label"], "level": transition, "value": value,
                         "threshold": threshold, "message": message}
                self.db.execute("INSERT INTO events(id,timestamp,payload) VALUES(?,?,?)", (event["id"], now_ms, json.dumps(event)))
        if now_ms - self.last_cleanup >= 60_000:
            self.db.execute("DELETE FROM buckets WHERE timestamp < ?", (now_ms - RETENTION_MS - 60_000,))
            self.db.execute("DELETE FROM events WHERE timestamp < ?", (now_ms - RETENTION_MS,))
            self.last_cleanup = now_ms
        self._save_state("alerts", self.alerts)
        self._save_state("catalog", self.catalog)
        self.db.commit()
        first = self.db.execute("SELECT MIN(timestamp) FROM buckets").fetchone()[0]
        active = []
        for key, state in self.alerts.items():
            if state.get("level", "normal") == "normal":
                continue
            descriptor = self.catalog.get(key, {})
            family = descriptor.get("family")
            rule = self.settings["rules"].get(family, {})
            active.append({"metric_id": key, "sensor": descriptor.get("label", key),
                           "level": state["level"], "value": state.get("value"),
                           "threshold": rule.get(state["level"]), "started_at": state.get("started_at"),
                           "last_seen": state.get("last_seen")})
        snapshot["monitoring"] = {"metrics": list(self.catalog.values()), "settings": self.settings,
                                  "active_alerts": active, "last_sample_at": now_ms,
                                  "history_started_at": first, "bucket_seconds": 60,
                                  "sample_seconds": 5}
        oldest = self.db.execute("SELECT timestamp,totals,samples,revision FROM buckets WHERE revision > sent_revision ORDER BY timestamp LIMIT 19").fetchall()
        latest = self.db.execute("SELECT timestamp,totals,samples,revision FROM buckets WHERE revision > sent_revision ORDER BY timestamp DESC LIMIT 1").fetchone()
        if latest and latest[0] not in {row[0] for row in oldest}:
            oldest.append(latest)
        snapshot["history_batch"] = [{"timestamp": stamp, "samples": count, "revision": revision,
                                      "values": {key: [round(value[0] / value[1], 3), value[2], value[3], value[4]] for key, value in json.loads(raw).items()}}
                                     for stamp, raw, count, revision in oldest]
        snapshot["alert_events"] = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM events WHERE sent=0 ORDER BY timestamp LIMIT 24")]

    def acknowledge(self, snapshot: dict):
        for bucket in snapshot.get("history_batch", []):
            self.db.execute("UPDATE buckets SET sent_revision=MAX(sent_revision,?) WHERE timestamp=?", (bucket["revision"], bucket["timestamp"]))
        for event in snapshot.get("alert_events", []):
            self.db.execute("UPDATE events SET sent=1 WHERE id=?", (event["id"],))
        self.db.commit()
