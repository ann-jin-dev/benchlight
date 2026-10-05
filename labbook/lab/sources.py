"""Read-only views of the records the other components already keep.

labbook never writes to these stores. gpuq's SQLite database is opened with
mode=ro (live WAL stays visible), the energy meter's database likewise, and
agent run folders are only read.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


def parse_time(value) -> float | None:
    """ISO-8601 (gpuq, codex-task) or epoch seconds -> epoch seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return dt.datetime.fromisoformat(str(value).strip()).timestamp()
    except ValueError:
        return None


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return default


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


# --------------------------------------------------------------------------- gpuq

class GpuqSource:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir).expanduser()

    def _db(self):
        return open_ro(self.root / "queue.sqlite3")

    @staticmethod
    def _decode(row) -> dict:
        job = dict(row)
        for key in ("spec", "allocation", "runtime"):
            job[key] = json.loads(job[key])
        return job

    def available(self) -> bool:
        return (self.root / "queue.sqlite3").is_file()

    def instance(self) -> str | None:
        db = self._db()
        if db is None:
            return None
        with db:
            row = db.execute("SELECT value FROM settings WHERE key='instance'").fetchone()
        db.close()
        return json.loads(row[0]) if row else None

    def jobs(self, limit: int | None = None, project: str | None = None) -> list[dict]:
        db = self._db()
        if db is None:
            return []
        query, args = "SELECT * FROM jobs", []
        if project:
            query, args = query + " WHERE project=?", [project]
        query += " ORDER BY id DESC"
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = [self._decode(row) for row in db.execute(query, args)]
        db.close()
        return rows

    def job(self, job_id: int) -> dict | None:
        db = self._db()
        if db is None:
            return None
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        job = self._decode(row) if row else None
        if job:
            job["dependencies"] = [r[0] for r in db.execute(
                "SELECT dependency_id FROM dependencies WHERE job_id=? ORDER BY dependency_id", (job_id,))]
        db.close()
        return job

    def run_dir(self, job_id: int) -> Path:
        return self.root / "runs" / f"{job_id:06d}"

    def artifacts(self, job_id: int) -> dict:
        run = self.run_dir(job_id)
        outputs = run / "outputs"
        files, size = 0, 0
        if outputs.is_dir():
            for path in outputs.rglob("*"):
                try:  # a running job may rotate or delete files meanwhile
                    if path.is_file():
                        size += path.stat().st_size
                        files += 1
                except OSError:
                    continue
                if files > 20000:
                    break
        try:
            log_bytes = (run / "console.log").stat().st_size
        except OSError:
            log_bytes = 0
        return {"run_dir": str(run), "output_files": files, "output_bytes": size, "log_bytes": log_bytes}

    @staticmethod
    def snapshot_summary(spec: dict) -> dict | None:
        """File count, bytes and one tree hash for a gpuq --snapshot source."""
        manifest = spec.get("source_snapshot")
        if not manifest:
            return None
        value = read_json(Path(manifest))
        if not value:
            return {"missing": True}
        lines, size = [], 0
        for entry in sorted(value.get("files", []), key=lambda item: item["path"]):
            if "sha256" in entry:
                lines.append(f"{entry['path']}\0{entry['sha256']}\n")
                size += entry.get("size", 0)
            else:
                lines.append(f"{entry['path']}\0->{entry.get('symlink', '')}\n")
        return {"files": len(lines), "bytes": size,
                "tree_sha256": hashlib.sha256("".join(lines).encode()).hexdigest(),
                "git": value.get("git")}


# --------------------------------------------------------------------------- pulse

class PulseSource:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir).expanduser()

    def latest(self) -> dict | None:
        return read_json(self.root / "latest.json")

    def job_energy(self, instance: str | None, job_id: int) -> dict | None:
        db = open_ro(self.root / "energy.sqlite3")
        if db is None:
            return None
        row = db.execute("SELECT * FROM jobs WHERE queue_instance=? AND job_id=?",
                         (instance or "", job_id)).fetchone()
        minutes = []
        if row:
            minutes = [dict(r) for r in db.execute(
                "SELECT minute, gpu_mj, cpu_mj, seconds FROM job_minutes "
                "WHERE queue_instance=? AND job_id=? ORDER BY minute", (instance or "", job_id))]
        db.close()
        if not row:
            return None
        return dict(row, minutes=minutes)

    def energy_by_job(self, instance: str | None) -> dict[int, dict]:
        db = open_ro(self.root / "energy.sqlite3")
        if db is None:
            return {}
        rows = {r["job_id"]: dict(r) for r in db.execute(
            "SELECT job_id, gpu_mj, cpu_mj, gpu_metered_seconds, cpu_metered_seconds "
            "FROM jobs WHERE queue_instance=?", (instance or "",))}
        db.close()
        return rows

    def days(self, limit: int = 30) -> list[dict]:
        db = open_ro(self.root / "energy.sqlite3")
        if db is None:
            return []
        rows = [dict(r) for r in db.execute("SELECT * FROM days ORDER BY day DESC LIMIT ?", (limit,))]
        db.close()
        return rows

    def history(self, metrics: list[str], since_ms: int) -> dict[str, list]:
        """Minute averages for selected metrics from System Pulse's history."""
        db = open_ro(self.root / "history.sqlite3")
        if db is None:
            return {}
        series = {key: [] for key in metrics}
        for stamp, raw, count in db.execute(
                "SELECT timestamp, totals, samples FROM buckets WHERE timestamp >= ? ORDER BY timestamp",
                (since_ms,)):
            totals = json.loads(raw)
            for key in metrics:
                value = totals.get(key)
                if value:
                    series[key].append([stamp, round(value[0] / value[1], 2)])
        db.close()
        return series


# --------------------------------------------------------------------------- agents

SUBMIT = re.compile(r"gpuq\S*\s+(?:--state-dir\s+\S+\s+)?submit\b")
JOB_ID_LINE = re.compile(r"^\s*(\d{1,9})\s*$", re.M)
STATUS = re.compile(r"STATUS:\s*([A-Z]+)")


class CodexSource:
    def __init__(self, runs_dir: Path, sessions_dir: Path):
        self.root = Path(runs_dir).expanduser()
        self.sessions = Path(sessions_dir).expanduser()

    def run_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted((p.parent.name for p in self.root.glob("*/meta.json")), reverse=True)

    def meta(self, run_id: str) -> dict | None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
            return None
        return read_json(self.root / run_id / "meta.json")

    def turn(self, run_id: str, n: int) -> dict:
        t = self.root / run_id / "turns" / str(n)
        ended = read_json(t / "ended") or {}
        final = read_text(t / "final.md").strip()
        statuses = STATUS.findall(final)
        started = parse_time(read_text(t / "started").strip())
        return {"n": n, "started": started, "ended": parse_time(ended.get("at")),
                "runner_alive": _runner_alive(read_text(t / "runner.pid").strip()),
                "last_activity": _mtime(t / "events.jsonl") or None,
                "end_source": ended.get("source"), "prompt": read_text(t / "prompt.md"),
                "final": final, "status": statuses[-1] if statuses else None,
                "exit_code": read_text(t / "exit_code").strip() or None,
                "turn_id": read_text(t / "turn_id").strip() or None,
                "steers": [{"at": p.stat().st_mtime, "text": read_text(p)}
                           for p in sorted((t / "steer").glob("*.sent"))] if (t / "steer").is_dir() else []}

    def turns(self, run_id: str) -> list[dict]:
        meta = self.meta(run_id) or {}
        return [self.turn(run_id, n) for n in range(1, int(meta.get("turns", 0)) + 1)]

    def activity(self, run_id: str, n: int) -> dict:
        """Commands, edits and gpuq submissions recorded in a turn's events."""
        path = self.root / run_id / "turns" / str(n) / "events.jsonl"
        return _activity(str(path), _mtime(path))

    def notes(self, run_id: str) -> list[dict]:
        notes = []
        for line in read_text(self.root / run_id / "notes.jsonl").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            entry["at_epoch"] = parse_time(entry.get("at"))
            notes.append(entry)
        return notes

    def wakes(self, run_id: str) -> list[dict]:
        wakes = []
        for line in read_text(self.root / run_id / "wake.log").splitlines():
            stamp, _, text = line.partition(" ")
            wakes.append({"at": parse_time(stamp), "text": text})
        return wakes

    def turn_tokens(self, thread_id: str | None, turn_id: str | None) -> dict | None:
        """Token usage of one Codex turn, from Codex's own session rollout."""
        if not thread_id or not turn_id or not self.sessions.is_dir():
            return None
        matches = sorted(self.sessions.glob(f"*/*/*/rollout-*{thread_id}.jsonl"))
        for path in matches:
            usage = _rollout_turn_usage(str(path), _mtime(path)).get(turn_id)
            if usage:
                return usage
        return None


def _runner_alive(pid: str) -> bool:
    """A codex-task turn runner still running (not just any process with that pid)."""
    if not pid.isdigit():
        return False
    try:
        return "codex-task" in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@functools.lru_cache(maxsize=256)
def _activity(path: str, _mtime_key: float) -> dict:
    commands = edits = messages = 0
    submissions = []
    try:
        stream = open(path, errors="replace")
    except OSError:
        return {"commands": 0, "edits": 0, "messages": 0, "submissions": []}
    with stream:
        for line in stream:
            if '"item"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item") or {}
            kind = item.get("type")
            if kind in ("commandExecution", "command_execution"):
                commands += 1
                command = str(item.get("command", ""))
                if SUBMIT.search(command) and "--help" not in command and str(item.get("exitCode")) == "0":
                    output = str(item.get("aggregatedOutput") or "")
                    ids = [int(x) for x in JOB_ID_LINE.findall(output)]
                    submissions.append({"job_ids": ids, "exit_code": item.get("exitCode")})
            elif kind in ("fileChange", "file_change"):
                edits += 1
            elif kind in ("agentMessage", "agent_message"):
                messages += 1
    return {"commands": commands, "edits": edits, "messages": messages, "submissions": submissions}


@functools.lru_cache(maxsize=64)
def _rollout_turn_usage(path: str, _mtime_key: float) -> dict[str, dict]:
    usage: dict[str, dict] = {}
    try:
        stream = open(path, errors="replace")
    except OSError:
        return usage
    with stream:
        for line in stream:
            if '"token_usage_record"' not in line:
                continue
            try:
                payload = json.loads(line).get("payload") or {}
            except ValueError:
                continue
            turn = payload.get("turn_id")
            totals = payload.get("turn_token_usage")
            if turn and isinstance(totals, dict):
                usage[turn] = {key: int(totals.get(key) or 0) for key in
                               ("input_tokens", "cached_input_tokens", "output_tokens",
                                "reasoning_output_tokens", "total_tokens")}
    return usage


def default_paths() -> dict:
    home = Path.home()
    state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    return {"gpuq_state_dir": os.environ.get("GPUQ_STATE_DIR", str(state / "gpuq")),
            "pulse_state_dir": str(state / "system-pulse"),
            "codex_runs_dir": os.environ.get("CODEX_RUNS_DIR", str(home / "codex-runs")),
            "codex_sessions_dir": str(home / ".codex/sessions")}
