"""Private SQLite state shared by the CLI and the worker."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid

ACTIVE = ("starting", "running", "cancelling")
TERMINAL = ("succeeded", "failed", "cancelled", "skipped")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", prefix=path.name + ".", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path, *, read_only: bool = False):
        self.root = root.expanduser().resolve()
        if read_only:
            # A cached queue query must not initialize state or change host
            # permissions. mode=ro keeps live WAL visibility; immutable=1 would
            # incorrectly hide updates made by the running worker.
            self.db = sqlite3.connect((self.root / "queue.sqlite3").as_uri() + "?mode=ro",
                                      uri=True, timeout=30, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA busy_timeout=30000")
            self.db.execute("PRAGMA query_only=ON")
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.db = sqlite3.connect(self.root / "queue.sqlite3", timeout=30,
                                  isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                project TEXT NOT NULL,
                name TEXT NOT NULL,
                priority INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                spec TEXT NOT NULL,
                allocation TEXT NOT NULL DEFAULT '[]',
                runtime TEXT NOT NULL DEFAULT '{}',
                exit_code INTEGER,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS dependencies (
                job_id INTEGER NOT NULL REFERENCES jobs(id),
                dependency_id INTEGER NOT NULL REFERENCES jobs(id),
                PRIMARY KEY(job_id, dependency_id)
            );
            CREATE INDEX IF NOT EXISTS jobs_state ON jobs(status, priority, id);
        """)
        (self.root / "queue.sqlite3").chmod(0o600)
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('instance', ?)",
                            (json.dumps(uuid.uuid4().hex),))

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def close(self):
        self.db.close()

    def setting(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value):
        self.db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    @staticmethod
    def decode(row):
        if row is None:
            return None
        job = dict(row)
        for key in ("spec", "allocation", "runtime"):
            job[key] = json.loads(job[key])
        return job

    def get(self, job_id: int):
        return self.decode(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def jobs(self, statuses=None):
        if statuses:
            rows = self.db.execute("SELECT * FROM jobs WHERE status IN (" +
                                   ",".join("?" for _ in statuses) + ") ORDER BY id", statuses)
        else:
            rows = self.db.execute("SELECT * FROM jobs ORDER BY id")
        return [self.decode(row) for row in rows]

    def update(self, job_id: int, **fields):
        allowed = {"status", "started_at", "finished_at", "spec", "allocation",
                   "runtime", "exit_code", "detail", "priority"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid job update")
        encoded = {key: json.dumps(value) if key in ("spec", "allocation", "runtime")
                   else value for key, value in fields.items()}
        self.db.execute("UPDATE jobs SET " + ", ".join(key + "=?" for key in encoded) +
                        " WHERE id=?", (*encoded.values(), job_id))

    def dependencies(self, job_id: int):
        return [row[0] for row in self.db.execute(
            "SELECT dependency_id FROM dependencies WHERE job_id=? ORDER BY dependency_id",
            (job_id,))]

    def enqueue(self, spec, dependencies=(), retry_of=None) -> int:
        with self.transaction():
            for dependency in dependencies:
                if self.get(dependency) is None:
                    raise ValueError(f"Dependency {dependency} does not exist")
            if retry_of is not None:
                spec = dict(spec, retry_of=retry_of)
            cursor = self.db.execute(
                "INSERT INTO jobs(status,project,name,priority,created_at,spec) "
                "VALUES ('queued',?,?,?,?,?)",
                (spec["project"], spec["name"], spec["priority"], now(), json.dumps(spec)))
            job_id = cursor.lastrowid
            for dependency in set(dependencies):
                self.db.execute("INSERT INTO dependencies VALUES (?,?)", (job_id, dependency))
        return job_id

    def run_dir(self, job_id: int) -> Path:
        return self.root / "runs" / f"{job_id:06d}"

    def prepare_run(self, job_id: int):
        run = self.run_dir(job_id)
        for path in (run, run / "outputs", run / "tmp"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        (run / "console.log").touch(mode=0o600, exist_ok=True)
        return run

    def manifest(self, job_id: int):
        job = self.get(job_id)
        if not job:
            return
        job["dependencies"] = self.dependencies(job_id)
        job["run_dir"] = str(self.run_dir(job_id))
        job["queue_instance"] = self.setting("instance")
        # Explicit environment values can include secrets; never copy them to reports.
        job["spec"]["env"] = {key: "<set>" for key in job["spec"].get("env", {})}
        atomic_json(self.run_dir(job_id) / "manifest.json", job)


def default_root() -> Path:
    return Path(os.environ.get("GPUQ_STATE_DIR", "~/.local/state/gpuq")).expanduser()


def load_config(root: Path):
    path = root.expanduser().resolve() / "config.json"
    if not path.is_file():
        raise ValueError(f"Queue is not configured: {path}. Run the installer on the host.")
    config = json.loads(path.read_text())
    if config.get("version") != 1:
        raise ValueError("Unsupported queue configuration version")
    gpu_ids = [gpu["uuid"] for gpu in config["gpus"]]
    if not gpu_ids or len(gpu_ids) != len(set(gpu_ids)):
        raise ValueError("Configuration needs unique GPU UUIDs")
    if config["max_cpus"] < 1 or config["max_memory_bytes"] < 1:
        raise ValueError("Invalid CPU or RAM budget")
    return config
