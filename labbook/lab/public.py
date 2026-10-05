"""The public window: what the lab is doing right now, safe to show anyone.

Everything here is built from an allowlist, never by removing fields from a
private record. It contains no hostnames, paths, commands, process names,
environment, job names, logs or agent transcripts. Real project names appear
only when the owner maps them to an alias; everything else is "private project".
"""

from __future__ import annotations

import time

from .core import Lab
from .sources import parse_time

STALE_SECONDS = 60


def alias(project: str, aliases: dict) -> str:
    return aliases.get(project) or "private project"


def gpu_card(gpu: dict, queue_gpus: dict) -> dict:
    state = queue_gpus.get(gpu.get("uuid"), {}).get("state")
    return {"name": gpu.get("name"), "utilization": gpu.get("utilization"),
            "temperature": gpu.get("temperature"), "power_w": gpu.get("power_w"),
            "vram_percent": round(100 * gpu["vram_used"] / gpu["vram_total"], 1)
            if gpu.get("vram_total") else None,
            "reserved": state == "reserved"}


def public_snapshot(lab: Lab, now: float | None = None) -> dict:
    now = now or time.time()
    settings = lab.config["public"]
    aliases = settings.get("project_aliases") or {}
    latest = lab.pulse.latest() or {}
    age = now - latest["timestamp"] if latest.get("timestamp") else None
    queue = latest.get("gpu_queue") or {}
    queue_gpus = {gpu["uuid"]: gpu for gpu in queue.get("gpus", [])}
    power = latest.get("power") or {}

    running = []
    for job in queue.get("jobs", []):
        if job.get("status") in ("starting", "running", "cancelling"):
            started = job.get("started_at")
            running.append({"project": alias(job.get("project", ""), aliases),
                            "gpus": job.get("gpu_count", 0),
                            "elapsed_seconds": round(now - started) if started else None})

    jobs = lab.gpuq.jobs()
    finished = sorted((job for job in jobs if job.get("finished_at")),
                      key=lambda job: parse_time(job["finished_at"]) or 0, reverse=True)
    gpu_seconds = 0.0
    for job in finished:
        started, ended = parse_time(job.get("started_at")), parse_time(job.get("finished_at"))
        if started and ended:
            gpu_seconds += (ended - started) * job["spec"].get("gpus", 0)
    first = min((parse_time(job["created_at"]) for job in jobs), default=None)

    runs = lab.runs()
    verdicts = sum(run["verdicts_decisions"] for run in runs)

    receipts = []
    energy = lab.pulse.energy_by_job(lab.gpuq.instance())
    for job in finished[: max(0, int(settings.get("show_recent_receipts", 8)))]:
        record = energy.get(job["id"])
        started, ended = parse_time(job.get("started_at")), parse_time(job.get("finished_at"))
        link = lab.link(job)
        receipts.append({"project": alias(job["project"], aliases), "status": job["status"],
                         "finished": ended, "gpus": job["spec"].get("gpus", 0),
                         "duration_seconds": round(ended - started) if started and ended else None,
                         "gpu_wh": round(record["gpu_mj"] / 3.6e6, 2) if record else None,
                         "agent": bool(link), "agent_link": link["link"] if link else None})

    days = [{"day": day["day"], "gpu_kwh": round(day["gpu_mj"] / 3.6e9, 3),
             "cpu_kwh": round(day["cpu_mj"] / 3.6e9, 3) if day["cpu_seconds"] else None,
             "attributed_gpu_kwh": round(day["job_gpu_mj"] / 3.6e9, 3)}
            for day in lab.pulse.days(14)]

    return {
        "title": settings.get("title"), "subtitle": settings.get("subtitle"),
        "repo_url": settings.get("repo_url") or "",
        "generated_at": now,
        "live": {"stale": age is None or age > STALE_SECONDS, "age_seconds": round(age) if age else None,
                 "gpus": [gpu_card(gpu, queue_gpus) for gpu in latest.get("gpus", [])],
                 "cpu_percent": (latest.get("cpu") or {}).get("total"),
                 "memory_percent": (latest.get("memory") or {}).get("percent"),
                 "power": {"gpu_w": power.get("gpu_total_w"), "cpu_w": power.get("cpu_package_w"),
                           "outlet_low_w": power.get("estimate_low_w"),
                           "outlet_high_w": power.get("estimate_high_w"),
                           "measured_w": power.get("measured_ac_w")},
                 "queue": {"running": running, "queued": sum(1 for job in queue.get("jobs", [])
                                                             if job.get("status") == "queued"),
                           "worker": queue.get("worker_state")},
                 "energy_today": (latest.get("energy") or {}).get("today")},
        "totals": {"jobs": len(jobs), "succeeded": sum(job["status"] == "succeeded" for job in jobs),
                   "gpu_hours": round(gpu_seconds / 3600, 1), "since": first,
                   "agent_runs": len(runs), "agent_turns": sum(run["turns"] for run in runs),
                   "recorded_verdicts_and_decisions": verdicts},
        "recent_receipts": receipts,
        "energy_days": days,
    }
