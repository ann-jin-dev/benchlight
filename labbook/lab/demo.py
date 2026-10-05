"""A synthetic lab for trying labbook without GPUs, agents or history.

`labbook demo` writes realistic but invented records in the same formats the
real components use (a gpuq queue database, codex-task run folders, Codex
token records, the energy meter's database and a System Pulse snapshot), then
serves the notebook and the public window on them.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import random
import sys
import time

from .config import DEFAULTS

ROOT = Path(__file__).resolve().parents[2]
GPUS = [{"index": 0, "uuid": "GPU-5f0c1d2e-demo-0000-0000-000000000000", "name": "NVIDIA GeForce RTX 5080"},
        {"index": 1, "uuid": "GPU-9a8b7c6d-demo-0000-0000-000000000001", "name": "NVIDIA GeForce RTX 5080"}]

PROTOCOL = "\n\n---\n## Operating protocol (from the supervising agent)\n(appended by codex-task)\n"

RUNS = [
    {
        "name": "vision-ablation-seeds", "project": "vision-ablation", "start_hours_ago": 30,
        "turns": [
            {"prompt": "# Does the patch-mixing augmentation help at small data?\n\n"
                       "**Question.** Decide whether patch-mixing stays in the training recipe for the 10% data regime.\n\n"
                       "**Tasks.**\n1. Reuse `configs/small_data.yaml`; train the baseline and the patch-mixing variant.\n"
                       "2. Five seeds each, one GPU per run, through gpuq.\n3. Report top-1 with a bootstrap 95% CI.\n\n"
                       "**Decision rule (fixed now).** Keep it if the CI of the difference excludes zero.\n\n"
                       "**Constraints.** At most 8 GPU-hours. Do not start the full-data runs.",
             "hours": 3.1, "status": "DONE",
             "summary": "Trained baseline and patch-mixing for 5 seeds each on the 10% split.\n"
                        "Patch-mixing improves top-1 by +2.4 points (95% CI +1.1 to +3.6), so it passes the rule.\n"
                        "One seed failed with an out-of-memory error and was retried at batch 96.",
             "jobs": [("prepare-split", 0, 4), *[(f"baseline-seed{s}", 1, 38) for s in range(1, 6)],
                      *[(f"patchmix-seed{s}", 1, 41) for s in range(1, 6)], ("patchmix-seed4-retry", 1, 43)],
             "fail": {"patchmix-seed4"}, "commands": 74, "edits": 6, "tokens": 1_840_000},
            {"prompt": "Verified your CSV: the gain holds per seed. Next: the same comparison at 25% data, "
                       "3 seeds, to see whether the effect shrinks with more data. Same rule, 4 GPU-hours.",
             "hours": 1.6, "status": "QUESTION",
             "summary": "At 25% data the gain is +0.9 points (95% CI -0.3 to +2.0); it no longer clears the rule.\n"
                        "Question: run 2 more seeds to narrow the CI, or stop here? Recommended: stop; the trend is clear.",
             "jobs": [*[(f"baseline25-seed{s}", 1, 52) for s in range(1, 4)],
                      *[(f"patchmix25-seed{s}", 1, 55) for s in range(1, 4)]],
             "commands": 41, "edits": 2, "tokens": 960_000},
            {"prompt": "Agree: stop at 3 seeds. Write the summary table and the plot to reports/patchmix.md.",
             "hours": 0.4, "status": "DONE",
             "summary": "Wrote reports/patchmix.md with the table, the per-seed plot and the decision record.",
             "jobs": [("make-figures", 0, 3)], "commands": 18, "edits": 3, "tokens": 310_000},
        ],
        "notes": [(1, 3.3, "claude", "verdict", "Recomputed the CI from results.csv: +2.4 [+1.1, +3.6]. Holds. The retry used the same seed and image."),
                  (2, 4.9, "human", "decision", "Keep patch-mixing for the small-data recipe; no more seeds at 25%."),
                  (3, 5.6, "claude", "verdict", "Report numbers match the CSVs; figures regenerated from the frozen source.")],
        "wake": (2, 4.75),
    },
    {
        "name": "probe-replication", "project": "llm-probing", "start_hours_ago": 7,
        "turns": [
            {"prompt": "# Replicate the linear-probe result on the 1B model\n\n**Question.** Does the layer-12 probe "
                       "accuracy reported in the reference paper reproduce with our extraction code?\n\n**Tasks.**\n"
                       "1. Extract activations for the 4 probing datasets (CPU preprocessing first).\n"
                       "2. Train probes per layer, 3 seeds.\n\n**Decision rule.** Reproduced if within 2 points of the reported 81.5%.",
             "hours": 2.2, "status": "DONE",
             "summary": "Layer-12 probe accuracy is 80.7% ± 0.6 over 3 seeds, within 2 points of 81.5%: reproduced.\n"
                        "Layers 10-14 form a plateau, which the paper does not mention.",
             "jobs": [("tokenize-datasets", 0, 9), ("extract-activations", 2, 47),
                      *[(f"probe-layers-seed{s}", 1, 21) for s in range(1, 4)]],
             "commands": 52, "edits": 4, "tokens": 1_210_000},
            {"prompt": "Good. Extend to the 3B model with the same protocol; report the plateau width too.",
             "hours": None, "status": None, "summary": None,
             "jobs": [("extract-activations-3b", 1, None), ("probe-layers-3b-seed1", 1, None)],
             "commands": 23, "edits": 1, "tokens": None},
        ],
        "notes": [(1, 2.4, "claude", "verdict", "Checked probe_results.json: 80.7 ± 0.6 at layer 12. Reproduced within the rule.")],
        "wake": None,
    },
]


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="microseconds")


def local_iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def build(root: Path, seed: int = 7) -> dict:
    sys.path.insert(0, str(ROOT / "gpuq"))
    from gpuqueue.store import Store

    rng = random.Random(seed)
    now = time.time()
    root.mkdir(parents=True, exist_ok=True)
    paths = {name: root / name for name in ("gpuq", "pulse", "runs", "sessions")}
    for path in paths.values():
        path.mkdir(exist_ok=True)
    store = Store(paths["gpuq"])
    instance = store.setting("instance")
    energy_rows, minute_rows, days = [], [], {}
    gpu_turn = 0
    running_jobs = []

    def hexid(text):
        return hashlib.sha256(f"{seed}:{text}".encode()).hexdigest()

    def add_job(project, name, gpus, minutes, created, cwd, origin, status="succeeded"):
        nonlocal gpu_turn
        image = f"{project}:2026.10"
        spec = {"project": project, "name": name, "priority": 0, "backend": "docker", "image": image,
                "command": ["python", "train.py" if gpus else "prepare.py", "--config", "configs/run.yaml"],
                "cwd": cwd, "original_cwd": cwd, "container_cwd": "/workspace/code", "gpus": gpus,
                "gpu_uuids": [], "cpus": 4 * max(1, gpus), "memory_bytes": 8 * 1024**3 * max(1, gpus),
                "shm_bytes": 2 * 1024**3, "env": {}, "env_files": [], "mounts": [], "entrypoint": None,
                "timeout_seconds": None, "cache_dir": None, "labels": {}, "origin": origin,
                "git_at_submission": {"commit": hexid(project)[:40], "dirty": False}}
        if rng.random() < 0.5:
            manifest = paths["gpuq"] / "snapshots" / f"source-{name}" / ".gpuq-source.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            files = [{"path": f"src/module_{i}.py", "size": 2000 + i * 97, "sha256": hexid(f"{project}{i}")} for i in range(24)]
            manifest.write_text(json.dumps({"origin": cwd, "git": spec["git_at_submission"], "files": files}))
            spec["source_snapshot"] = str(manifest)
        job_id = store.enqueue(spec)
        started = created + rng.uniform(2, 40)
        allocated = [GPUS[(gpu_turn + k) % 2] for k in range(gpus)]
        gpu_turn += 1
        finished = started + minutes * 60 if minutes else None
        runtime = {"instance": instance, "image_id": "sha256:" + hexid(image)[:64], "name": f"gpuq-demo-{job_id}",
                   "gpus": allocated}
        fields = {"started_at": iso(started), "runtime": runtime, "allocation": [g["uuid"] for g in allocated]}
        if finished and finished < now:
            fields.update(status=status, finished_at=iso(finished), exit_code=0 if status == "succeeded" else 1,
                          detail="Completed" if status == "succeeded" else "Command exited with status 1")
        else:
            fields.update(status="running", detail="Running")
            running_jobs.append((job_id, project, name, gpus, started, allocated))
        store.update(job_id, **fields)
        store.db.execute("UPDATE jobs SET created_at=? WHERE id=?", (iso(created), job_id))
        end = min(finished or now, now)
        if end - started > 60:
            gpu_w = rng.uniform(205, 265) if gpus else 0.0
            cpu_w = rng.uniform(18, 42)
            gpu_mj = cpu_mj = 0.0
            minute = int(started // 60 * 60)
            while minute < end:
                seconds = min(60, end - max(minute, started))
                g = (gpu_w * gpus * rng.uniform(0.92, 1.05)) * seconds * 1000
                c = cpu_w * rng.uniform(0.85, 1.1) * seconds * 1000
                minute_rows.append((instance, job_id, minute, g, c, seconds))
                gpu_mj += g
                cpu_mj += c
                day = dt.datetime.fromtimestamp(minute).astimezone().date().isoformat()
                totals = days.setdefault(day, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
                totals[0] += g
                totals[1] += c * 2.4
                totals[2] += g
                totals[3] += c
                minute += 60
            energy_rows.append((instance, job_id, project, ",".join(g["uuid"] for g in allocated), gpu_mj, cpu_mj,
                                (end - started) * 0.9, end - started, end - started, started, end))
        return job_id

    for run_spec in RUNS:
        start = now - run_spec["start_hours_ago"] * 3600
        cwd = f"/home/demo/projects/{run_spec['project']}"
        run_id = dt.datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M%S") + "-" + run_spec["name"]
        run_dir = paths["runs"] / run_id
        thread = hexid(run_id)[:8] + "-demo-thread"
        cursor = start
        for n, turn in enumerate(run_spec["turns"], 1):
            directory = run_dir / "turns" / str(n)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "prompt.md").write_text(turn["prompt"] + (PROTOCOL if n == 1 else ""))
            (directory / "started").write_text(local_iso(cursor))
            (directory / "turn_id").write_text(f"{thread}-turn-{n}")
            events = []
            job_time = cursor + 600
            for index, (name, gpus, minutes) in enumerate(turn["jobs"]):
                exact = index % 3 != 2  # some jobs come from scripts: linked by time and folder
                origin = {"codex_run": run_id, "codex_turn": str(n)} if exact else {}
                status = "failed" if name in turn.get("fail", set()) else "succeeded"
                job_id = add_job(run_spec["project"], name, gpus, minutes, job_time, cwd, origin, status)
                if exact:
                    events.append({"type": "item", "item": {"type": "commandExecution",
                                   "command": f"/bin/bash -lc 'gpuq submit --project {run_spec['project']} --gpus {gpus} -- python train.py'",
                                   "aggregatedOutput": f"{job_id}\n", "exitCode": 0}})
                job_time += rng.uniform(30, 300) if gpus else (minutes or 5) * 60 + 30
            for _ in range(turn["commands"]):
                events.append({"type": "item", "item": {"type": "commandExecution", "command": "/bin/bash -lc 'ls'", "exitCode": 0}})
            for _ in range(turn["edits"]):
                events.append({"type": "item", "item": {"type": "fileChange", "changes": [{"path": "scripts/run.py"}]}})
            (directory / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            if turn["hours"]:
                ended = cursor + turn["hours"] * 3600
                (directory / "ended").write_text(json.dumps({"source": "runner", "rc": 0, "at": local_iso(ended)}))
                (directory / "exit_code").write_text("0")
                (directory / "final.md").write_text(
                    f"## Summary\n{turn['summary']}\n## Results\nSee reports/.\n## Decisions made\nNone beyond the brief.\n"
                    f"## Artifacts\nreports/\n## Next\nAwaiting the supervisor.\nSTATUS: {turn['status']}\n")
                session = paths["sessions"] / dt.datetime.fromtimestamp(start).strftime("%Y/%m/%d")
                session.mkdir(parents=True, exist_ok=True)
                with open(session / f"rollout-demo-{thread}.jsonl", "a") as stream:
                    total = turn["tokens"]
                    stream.write(json.dumps({"type": "token_usage_record", "payload": {
                        "turn_id": f"{thread}-turn-{n}", "turn_token_usage": {
                            "input_tokens": int(total * 0.97), "cached_input_tokens": int(total * 0.9),
                            "output_tokens": int(total * 0.03), "reasoning_output_tokens": int(total * 0.012),
                            "total_tokens": total}}}) + "\n")
                cursor = ended + rng.uniform(300, 1800)
        (run_dir / "meta.json").write_text(json.dumps({
            "id": run_id, "name": run_spec["name"], "cwd": cwd, "created": local_iso(start), "model": "codex-default",
            "effort": "high", "sandbox": None, "thread_id": thread, "turns": len(run_spec["turns"]),
            "claude_session": "demo", "wakes": 1 if run_spec["wake"] else 0, "max_wakes": 20}, indent=2))
        with open(run_dir / "notes.jsonl", "w") as stream:
            for turn_n, hours, by, kind, text in run_spec["notes"]:
                stream.write(json.dumps({"at": local_iso(start + hours * 3600), "by": by, "kind": kind,
                                         "turn": turn_n, "text": text}) + "\n")
        if run_spec["wake"]:
            turn_n, hours = run_spec["wake"]
            (run_dir / "wake.log").write_text(
                f"{local_iso(start + hours * 3600)} turn {turn_n}: waking Claude headless (1/20)\n")

    # A few jobs a person submitted directly.
    for k in range(6):
        add_job("rl-sweep", f"ppo-lr{k}", 1, rng.uniform(20, 70), now - rng.uniform(10, 26) * 3600,
                "/home/demo/projects/rl-sweep", {})
    store.close()

    import sqlite3
    db = sqlite3.connect(paths["pulse"] / "energy.sqlite3")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (queue_instance TEXT NOT NULL, job_id INTEGER NOT NULL, project TEXT,
            gpu_uuids TEXT, gpu_mj REAL NOT NULL DEFAULT 0, cpu_mj REAL NOT NULL DEFAULT 0,
            cpu_seconds REAL NOT NULL DEFAULT 0, gpu_metered_seconds REAL NOT NULL DEFAULT 0,
            cpu_metered_seconds REAL NOT NULL DEFAULT 0, first_at REAL, last_at REAL,
            PRIMARY KEY (queue_instance, job_id));
        CREATE TABLE IF NOT EXISTS job_minutes (queue_instance TEXT NOT NULL, job_id INTEGER NOT NULL,
            minute INTEGER NOT NULL, gpu_mj REAL NOT NULL DEFAULT 0, cpu_mj REAL NOT NULL DEFAULT 0,
            seconds REAL NOT NULL DEFAULT 0, PRIMARY KEY (queue_instance, job_id, minute));
        CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, gpu_mj REAL NOT NULL DEFAULT 0,
            cpu_mj REAL NOT NULL DEFAULT 0, job_gpu_mj REAL NOT NULL DEFAULT 0, job_cpu_mj REAL NOT NULL DEFAULT 0,
            gpu_seconds REAL NOT NULL DEFAULT 0, cpu_seconds REAL NOT NULL DEFAULT 0);
    """)
    db.executemany("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)", energy_rows)
    db.executemany("INSERT OR REPLACE INTO job_minutes VALUES (?,?,?,?,?,?)", minute_rows)
    for day, (g, c, jg, jc, _, _) in days.items():
        idle = 2 * 22 * 86400 * 1000 * 0.35  # idle draw of two cards for part of the day
        db.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?,?)", (day, g + idle, c, jg, jc, 86400, 86400))
    db.commit()
    db.close()

    today = days.get(dt.datetime.fromtimestamp(now).astimezone().date().isoformat(), [0.0] * 6)
    running_ids = {job[0] for job in running_jobs}
    energy = {"jobs": {str(row[1]): {"gpu_wh": round(row[4] / 3.6e6, 3), "cpu_wh": round(row[5] / 3.6e6, 3),
                                     "gpu_metered_seconds": row[7], "cpu_metered_seconds": row[8]}
                       for row in energy_rows if row[1] in running_ids},
              "today": {"gpu_wh": round((today[0] + 2 * 22 * 3600 * 1000 * 4) / 3.6e6, 2),
                        "cpu_package_wh": round(today[1] / 3.6e6, 2),
                        "job_gpu_wh": round(today[2] / 3.6e6, 2), "job_cpu_wh": round(today[3] / 3.6e6, 2)},
              "cpu_measured": True}
    write_snapshot(paths["pulse"], running_jobs, instance, now, rng, energy, store_next=max(running_ids, default=0) + 1)
    config = copy.deepcopy(DEFAULTS)
    config.update(gpuq_state_dir=str(paths["gpuq"]), pulse_state_dir=str(paths["pulse"]),
                  codex_runs_dir=str(paths["runs"]), codex_sessions_dir=str(paths["sessions"]))
    config["electricity"].update(price_per_kwh=0.30, grid_gco2_per_kwh=250,
                                 grid_source="demo values; set your own utility rate and grid intensity")
    config["public"].update(subtitle="Demo data: an invented week on a two-GPU workstation.",
                            project_aliases={"vision-ablation": "Vision study", "llm-probing": "Language-model probing"})
    return config


def write_snapshot(pulse_dir: Path, running_jobs, instance, now, rng, energy, store_next):
    """A System Pulse latest.json for the demo's running jobs."""
    held = {g["uuid"]: job for job in running_jobs for g in job[5]}
    gpus = []
    for gpu in GPUS:
        busy = gpu["uuid"] in held
        gpus.append(dict(gpu, temperature=round(rng.uniform(61, 69) if busy else 38, 1), fan_percent=48 if busy else 30,
                         fan_channels=[], utilization=round(rng.uniform(94, 100)) if busy else 0, memory_utilization=40,
                         vram_used=int((rng.uniform(5, 11) if busy else 0.4) * 1024**3), vram_total=16 * 1024**3,
                         power_w=round(rng.uniform(215, 255) if busy else 21, 1)))
    jobs = [{"id": job_id, "name": name, "project": project, "status": "running", "priority": 0, "detail": "",
             "gpu_count": gpus_n, "cpus": 4.0, "memory_bytes": 8 * 1024**3, "gpu_uuids": [g["uuid"] for g in allocated],
             "created_at": started - 20, "started_at": started}
            for job_id, project, name, gpus_n, started, allocated in running_jobs]
    gpu_w = sum(g["power_w"] for g in gpus)
    snapshot = {
        "timestamp": now, "hostname": "demo-workstation", "uptime_seconds": 6 * 86400 + 5000,
        "cpu": {"total": 14.2, "cores": []}, "load": [2.1, 2.3, 2.2],
        "memory": {"used": 18 * 1024**3, "total": 31 * 1024**3, "percent": 58.1},
        "disk": {"used": 1.1 * 1024**4, "total": 1.8 * 1024**4, "percent": 61.0},
        "network": {"down_bps": 120000, "up_bps": 30000, "interfaces": []},
        "sensors": {"temperatures": [{"name": "CPU package", "chip": "k10temp", "celsius": 71.5},
                                     {"name": "SSD", "chip": "nvme", "celsius": 44.0}], "fans": []},
        "gpus": gpus,
        "power": {"cpu_package_w": 68.0, "gpu_total_w": gpu_w, "monitored_w": gpu_w + 68,
                  "estimate_low_w": round((gpu_w + 120) / 5) * 5, "estimate_high_w": round((gpu_w + 190) / 5) * 5,
                  "measured_ac_w": None, "modeled_components": [], "profile": "x870-dual-rtx5080",
                  "psu": "Thermaltake Toughpower GT 1200 W (TPD-1200AH2FXG-3)"},
        "gpu_queue": {"worker_state": "running", "paused": False, "counts": {"running": len(jobs), "queued": 1},
                      "gpus": [{"index": g["index"], "uuid": g["uuid"], "name": g["name"],
                                "state": "reserved" if g["uuid"] in held else "available",
                                "job_id": held[g["uuid"]][0] if g["uuid"] in held else None, "detail": ""} for g in GPUS],
                      "jobs": jobs + [{"id": store_next + 6, "name": "probe-layers-3b-seed2", "project": "llm-probing", "status": "queued",
                                       "priority": 0, "detail": "Waiting: needs 1 GPU(s); 0 available", "gpu_count": 1,
                                       "cpus": 4.0, "memory_bytes": 8 * 1024**3, "gpu_uuids": [], "created_at": now - 600,
                                       "started_at": None}]},
        "processes": [], "experiments": [], "availability": {}, "warnings": [],
        "energy": energy,
    }
    (pulse_dir / "latest.json").write_text(json.dumps(snapshot))


def serve(directory: Path | None, port: int, public_port: int):
    import tempfile
    import threading

    from .core import Lab
    from .server import PrivateHandler, PublicHandler, run

    root = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="labbook-demo-"))
    lab = Lab(build(root))
    print(f"Demo records in {root}")

    def keep_live():  # the demo machine "samples" every five seconds
        rng = random.Random()
        latest = Path(lab.config["pulse_state_dir"]) / "latest.json"
        while True:
            time.sleep(5)
            snapshot = json.loads(latest.read_text())
            snapshot["timestamp"] = time.time()
            for gpu in snapshot["gpus"]:
                if gpu["utilization"]:
                    gpu["utilization"] = rng.randint(93, 100)
                    gpu["power_w"] = round(rng.uniform(215, 255), 1)
            latest.write_text(json.dumps(snapshot))

    threading.Thread(target=keep_live, daemon=True).start()
    print(f"Public window:   http://127.0.0.1:{public_port}/")
    threading.Thread(target=run, args=(PublicHandler, lab, "127.0.0.1", public_port), daemon=True).start()
    run(PrivateHandler, lab, "127.0.0.1", port)
