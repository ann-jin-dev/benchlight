"""Experiment receipts: what one GPU job ran on, what it cost, and who asked.

A receipt joins records that already exist: gpuq's job and manifest (code,
image, hardware, timing), System Pulse's energy meter (measured GPU energy,
attributed CPU energy), and the agent run that submitted the job (Codex turn,
token usage, verdicts and decisions). Each finished receipt carries a SHA-256
digest over the parts that cannot change once the job has ended (DIGEST_SCOPE),
so a shared copy can be checked with `labbook verify`. The agent section (notes
may be added later), outputs and pricing (the owner's tariff) stay outside it.
"""

from __future__ import annotations

import hashlib
import json
import time

from .sources import parse_time

RECEIPT_VERSION = 1
TERMINAL = ("succeeded", "failed", "cancelled", "skipped")
DIGEST_SCOPE = ("receipt_version", "id", "job", "times", "code", "environment", "hardware", "energy")


def _canonical(value):
    """Integral floats become ints, so a copy that passed through JavaScript
    (where 1.0 is written as 1) hashes the same."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def canonical_digest(receipt: dict) -> str:
    body = _canonical({key: receipt.get(key) for key in DIGEST_SCOPE})
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def times(job: dict, now: float | None = None) -> dict:
    created = parse_time(job.get("created_at"))
    started = parse_time(job.get("started_at"))
    finished = parse_time(job.get("finished_at"))
    end = finished or (now or time.time()) if started else None
    return {"submitted": created, "started": started, "finished": finished,
            "queue_wait_seconds": round(started - created, 1) if started and created else None,
            "duration_seconds": round(end - started, 1) if started and end else None}


def energy_section(energy: dict | None, duration: float | None, gpus: int) -> dict | None:
    if not energy:
        return None
    gpu_wh = energy["gpu_mj"] / 3.6e6
    cpu_wh = energy["cpu_mj"] / 3.6e6
    cpu_measured = energy["cpu_metered_seconds"] > 0
    total_wh = gpu_wh + cpu_wh

    def coverage(seconds):
        return round(min(1.0, seconds / duration), 3) if duration else None

    gpu_coverage = coverage(energy["gpu_metered_seconds"]) if gpus else None
    cpu_coverage = coverage(energy["cpu_metered_seconds"]) if cpu_measured else None
    section = {
        "gpu_wh": round(gpu_wh, 3), "cpu_wh": round(cpu_wh, 3) if cpu_measured else None,
        "total_wh": round(total_wh, 3),
        # The share of the run that was metered: by the cards for GPU jobs, by RAPL otherwise.
        "coverage": gpu_coverage if gpus else (cpu_coverage or 0.0),
        "gpu_coverage": gpu_coverage, "cpu_coverage": cpu_coverage,
        "cpu_seconds": round(energy.get("cpu_seconds", 0), 1),
        "method": {"gpu": "measured: NVML energy counter of each exclusively reserved card",
                   "cpu": ("attributed: RAPL package energy × the job's share of busy CPU time"
                           if cpu_measured else "not measured: RAPL counter unreadable")},
        "power_minutes": [[m["minute"], round((m["gpu_mj"] + m["cpu_mj"]) / 1000 / max(m["seconds"], 1), 1)]
                          for m in energy.get("minutes", []) if m["seconds"] > 0],
    }
    return section


def pricing_section(energy: dict | None, electricity: dict) -> dict | None:
    """Cost and CO2 from the owner's own tariff and grid; never guessed."""
    if not energy:
        return None
    pricing = {}
    price = electricity.get("price_per_kwh")
    intensity = electricity.get("grid_gco2_per_kwh")
    if price is not None:
        pricing["cost"] = {"amount": round(energy["total_wh"] / 1000 * price, 4),
                           "currency": electricity.get("currency", "USD"), "price_per_kwh": price}
    if intensity is not None:
        pricing["co2_grams"] = round(energy["total_wh"] / 1000 * intensity, 1)
        pricing["grid_gco2_per_kwh"] = intensity
        pricing["grid_source"] = electricity.get("grid_source", "")
    return pricing or None


def build_receipt(job: dict, *, instance: str | None, energy: dict | None, agent: dict | None,
                  snapshot: dict | None, artifacts: dict | None, electricity: dict,
                  now: float | None = None) -> dict:
    spec, runtime = job["spec"], job["runtime"]
    when = times(job, now)
    git = spec.get("git_at_submission") or (snapshot or {}).get("git")
    measured = energy_section(energy, when["duration_seconds"], spec.get("gpus", 0))
    body = {
        "receipt_version": RECEIPT_VERSION,
        "id": f"{(instance or 'local')[:8]}-{job['id']}",
        "final": job["status"] in TERMINAL,
        "job": {"id": job["id"], "project": job["project"], "name": job["name"],
                "status": job["status"], "exit_code": job.get("exit_code"),
                "detail": job.get("detail", ""), "labels": spec.get("labels") or {},
                "retry_of": spec.get("retry_of"), "dependencies": job.get("dependencies", []),
                "priority": job.get("priority", 0)},
        "times": when,
        "code": {"git_commit": (git or {}).get("commit"), "git_dirty": (git or {}).get("dirty"),
                 "snapshot": snapshot, "cwd": spec.get("original_cwd") or spec.get("cwd")},
        "environment": {"backend": spec.get("backend"), "image": spec.get("image"),
                        "image_id": runtime.get("image_id"), "command": spec.get("command"),
                        "mounts": len(spec.get("mounts") or [])},
        "hardware": {"gpus": [{"index": g.get("index"), "name": g.get("name"),
                               "uuid": (g.get("uuid") or "")[:12]} for g in runtime.get("gpus", [])],
                     "cpus_reserved": spec.get("cpus"), "memory_reserved_bytes": spec.get("memory_bytes")},
        "energy": measured,
        "pricing": pricing_section(measured, electricity),
        "agent": agent,
        "artifacts": artifacts,
        "digest_scope": list(DIGEST_SCOPE),
    }
    body["digest"] = canonical_digest(body) if body["final"] else None
    return body


def verify(receipt: dict) -> bool:
    """Whether a receipt (for example a downloaded copy) still matches its digest."""
    return bool(receipt.get("digest")) and canonical_digest(receipt) == receipt["digest"]


def summary_row(job: dict, energy: dict | None, agent: dict | None, now: float | None = None) -> dict:
    """A light row for receipt lists."""
    when = times(job, now)
    return {"id": job["id"], "project": job["project"], "name": job["name"], "status": job["status"],
            "submitted": when["submitted"], "started": when["started"], "finished": when["finished"],
            "duration_seconds": when["duration_seconds"], "gpus": job["spec"].get("gpus", 0),
            "gpu_wh": round(energy["gpu_mj"] / 3.6e6, 2) if energy else None,
            "cpu_wh": round(energy["cpu_mj"] / 3.6e6, 2) if energy and energy["cpu_metered_seconds"] else None,
            "agent": {"run": agent.get("run"), "turn": agent.get("turn"), "link": agent["link"],
                      "claude_session": agent.get("claude_session")} if agent else None}
