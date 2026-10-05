"""Per-job energy accounting for gpuq jobs.

GPU energy is measured, not modelled: NVML exposes a cumulative millijoule
counter per card, and gpuq reserves cards exclusively, so every millijoule a
card uses while a job holds it belongs to that job (idle draw included).

CPU energy is attributed: the RAPL package counter is shared by everything on
the machine, so each interval's package energy is split by each job's share of
busy CPU time, read from the job's own cgroup.

Intervals with a gap, a reboot, or a counter reset are skipped and counted as
unmetered time, so a receipt can say how much of a run it actually covers.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import sqlite3
import subprocess

TICKS = os.sysconf("SC_CLK_TCK")
CGROUP_ROOT = Path("/sys/fs/cgroup")


def default_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state / "system-pulse" / "energy.sqlite3"


def host_busy_usec(stat_text: str | None = None) -> int | None:
    """Busy CPU time of the whole machine (user+nice+system+irq+softirq+steal)."""
    try:
        text = stat_text if stat_text is not None else Path("/proc/stat").read_text()
        fields = [int(value) for value in text.splitlines()[0].split()[1:9]]
    except (OSError, ValueError, IndexError):
        return None
    user, nice, system, _idle, _iowait, irq, softirq, steal = fields
    return (user + nice + system + irq + softirq + steal) * 1_000_000 // TICKS


def cgroup_usage_usec(cgroup: str | None) -> int | None:
    if not cgroup:
        return None
    try:
        for line in (CGROUP_ROOT / cgroup.lstrip("/") / "cpu.stat").read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "usage_usec":
                return int(value)
    except (OSError, ValueError):
        return None
    return None


class CgroupLocator:
    """Find the cgroup of a gpuq job's container or transient systemd unit."""

    def __init__(self):
        self.cache: dict[str, str] = {}

    def __call__(self, job: dict) -> str | None:
        name = (job.get("runtime") or {}).get("name")
        if not name:
            return None
        cached = self.cache.get(name)
        if cached and (CGROUP_ROOT / cached.lstrip("/")).is_dir():
            return cached
        backend = (job.get("spec") or {}).get("backend")
        cgroup = self._docker(name) if backend == "docker" else self._native(name)
        if cgroup:
            self.cache[name] = cgroup
        return cgroup

    @staticmethod
    def _run(argv):
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""

    def _docker(self, name):
        pid = self._run(["docker", "inspect", "--type", "container", "--format", "{{.State.Pid}}", name])
        if not pid.isdigit() or pid == "0":
            return None
        try:
            for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
                if line.startswith("0::"):
                    return line[3:]
        except OSError:
            return None
        return None

    def _native(self, name):
        return self._run(["systemctl", "--user", "show", "--property=ControlGroup", "--value",
                          name + ".service"]) or None

    def forget(self, active_names):
        for name in set(self.cache) - set(active_names):
            del self.cache[name]


def readings_from(snapshot: dict, energy: dict, gpuq_status: dict | None, locate) -> dict:
    """Assemble one meter reading from a collector snapshot and raw gpuq status."""
    jobs = []
    for job in (gpuq_status or {}).get("jobs", []):
        if job.get("status") != "running":
            continue
        cgroup = locate(job)
        jobs.append({"id": int(job["id"]), "instance": (job.get("runtime") or {}).get("instance"),
                     "project": job.get("project"), "gpus": list(job.get("allocation") or []),
                     "cpu_usec": cgroup_usage_usec(cgroup)})
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot = None
    return {"time": snapshot["timestamp"], "boot_id": boot,
            "gpu_mj": {gpu["uuid"]: gpu["energy_mj"] for gpu in snapshot.get("gpus", [])
                       if gpu.get("energy_mj") is not None},
            "rapl_uj": energy.get("rapl_uj"), "rapl_max_uj": energy.get("rapl_max_uj"),
            "host_busy_usec": host_busy_usec(), "jobs": jobs}


class EnergyMeter:
    def __init__(self, path: Path | str | None = None, max_gap_seconds: float = 30):
        path = Path(path) if path else default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                queue_instance TEXT NOT NULL, job_id INTEGER NOT NULL, project TEXT, gpu_uuids TEXT,
                gpu_mj REAL NOT NULL DEFAULT 0, cpu_mj REAL NOT NULL DEFAULT 0,
                cpu_seconds REAL NOT NULL DEFAULT 0,
                gpu_metered_seconds REAL NOT NULL DEFAULT 0,
                cpu_metered_seconds REAL NOT NULL DEFAULT 0,
                first_at REAL, last_at REAL, PRIMARY KEY (queue_instance, job_id)
            );
            CREATE TABLE IF NOT EXISTS job_minutes (
                queue_instance TEXT NOT NULL, job_id INTEGER NOT NULL, minute INTEGER NOT NULL,
                gpu_mj REAL NOT NULL DEFAULT 0, cpu_mj REAL NOT NULL DEFAULT 0,
                seconds REAL NOT NULL DEFAULT 0, PRIMARY KEY (queue_instance, job_id, minute)
            );
            CREATE TABLE IF NOT EXISTS days (
                day TEXT PRIMARY KEY,
                gpu_mj REAL NOT NULL DEFAULT 0, cpu_mj REAL NOT NULL DEFAULT 0,
                job_gpu_mj REAL NOT NULL DEFAULT 0, job_cpu_mj REAL NOT NULL DEFAULT 0,
                gpu_seconds REAL NOT NULL DEFAULT 0, cpu_seconds REAL NOT NULL DEFAULT 0
            );
        """)
        self.max_gap = max_gap_seconds
        self.previous: dict | None = None

    def close(self):
        self.db.close()

    def observe(self, reading: dict) -> dict:
        previous, self.previous = self.previous, reading
        if previous is None or previous.get("boot_id") != reading.get("boot_id"):
            return self.summary(reading)
        elapsed = reading["time"] - previous["time"]
        if not 0 < elapsed <= self.max_gap:
            return self.summary(reading)

        gpu_delta = {}
        for uuid, counter in reading["gpu_mj"].items():
            before = previous["gpu_mj"].get(uuid)
            # A driver reload resets the counter; >3 kW per card is not a real reading.
            if before is not None and 0 <= counter - before <= 3_000_000 * elapsed:
                gpu_delta[uuid] = counter - before

        package_mj = None
        if None not in (reading["rapl_uj"], previous["rapl_uj"]) and reading["rapl_max_uj"]:
            delta = (reading["rapl_uj"] - previous["rapl_uj"]) % reading["rapl_max_uj"]
            if delta <= 1_000_000_000 * elapsed:  # under 1 kW for a CPU package
                package_mj = delta / 1000
        host_busy = None
        if reading["host_busy_usec"] is not None and previous["host_busy_usec"] is not None:
            host_busy = reading["host_busy_usec"] - previous["host_busy_usec"]

        stamp = dt.datetime.fromtimestamp(reading["time"]).astimezone()
        minute = int(reading["time"] // 60 * 60)
        day = stamp.date().isoformat()
        before_jobs = {(job["instance"], job["id"]): job for job in previous["jobs"]}
        job_gpu_total = job_cpu_total = 0.0
        with self.db:
            for job in reading["jobs"]:
                old = before_jobs.get((job["instance"], job["id"]))
                if old is None:
                    continue  # first sighting: no interval yet
                gpu_mj = None  # None: not measured this interval
                if job["gpus"] and all(uuid in gpu_delta for uuid in job["gpus"]):
                    gpu_mj = sum(gpu_delta[uuid] for uuid in job["gpus"])
                cpu_mj = cpu_seconds = None
                if job["cpu_usec"] is not None and old["cpu_usec"] is not None:
                    used = max(0, job["cpu_usec"] - old["cpu_usec"])
                    cpu_seconds = used / 1_000_000
                    if package_mj is not None and host_busy is not None:
                        cpu_mj = package_mj * min(1.0, used / host_busy) if host_busy > 0 else 0.0
                job_gpu_total += gpu_mj or 0
                job_cpu_total += cpu_mj or 0
                # GPU jobs are metered by their cards; CPU-only jobs by RAPL.
                metered = gpu_mj is not None if job["gpus"] else cpu_mj is not None
                self.db.execute("""
                    INSERT INTO jobs(queue_instance, job_id, project, gpu_uuids, first_at, last_at)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(queue_instance, job_id) DO UPDATE SET last_at=excluded.last_at
                """, (job["instance"] or "", job["id"], job["project"], ",".join(job["gpus"]),
                      previous["time"], reading["time"]))
                self.db.execute("""
                    UPDATE jobs SET gpu_mj = gpu_mj + ?, cpu_mj = cpu_mj + ?, cpu_seconds = cpu_seconds + ?,
                        gpu_metered_seconds = gpu_metered_seconds + ?,
                        cpu_metered_seconds = cpu_metered_seconds + ?
                    WHERE queue_instance = ? AND job_id = ?
                """, (gpu_mj or 0, cpu_mj or 0, cpu_seconds or 0,
                      elapsed if gpu_mj is not None else 0, elapsed if cpu_mj is not None else 0,
                      job["instance"] or "", job["id"]))
                if not metered:
                    continue
                self.db.execute("""
                    INSERT INTO job_minutes(queue_instance, job_id, minute, gpu_mj, cpu_mj, seconds)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(queue_instance, job_id, minute) DO UPDATE SET
                        gpu_mj = gpu_mj + excluded.gpu_mj, cpu_mj = cpu_mj + excluded.cpu_mj,
                        seconds = seconds + excluded.seconds
                """, (job["instance"] or "", job["id"], minute, gpu_mj or 0, cpu_mj or 0, elapsed))
            self.db.execute("""
                INSERT INTO days(day, gpu_mj, cpu_mj, job_gpu_mj, job_cpu_mj, gpu_seconds, cpu_seconds)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET
                    gpu_mj = gpu_mj + excluded.gpu_mj, cpu_mj = cpu_mj + excluded.cpu_mj,
                    job_gpu_mj = job_gpu_mj + excluded.job_gpu_mj,
                    job_cpu_mj = job_cpu_mj + excluded.job_cpu_mj,
                    gpu_seconds = gpu_seconds + excluded.gpu_seconds,
                    cpu_seconds = cpu_seconds + excluded.cpu_seconds
            """, (day, sum(gpu_delta.values()), package_mj or 0, job_gpu_total, job_cpu_total,
                  elapsed if gpu_delta else 0, elapsed if package_mj is not None else 0))
        return self.summary(reading)

    def summary(self, reading: dict) -> dict:
        """Energy so far for running jobs and today's machine totals, in watt-hours."""
        jobs = {}
        for job in reading["jobs"]:
            row = self.db.execute("SELECT job_id, gpu_mj, cpu_mj, gpu_metered_seconds, cpu_metered_seconds "
                                  "FROM jobs WHERE queue_instance = ? AND job_id = ?",
                                  (job["instance"] or "", job["id"])).fetchone()
            if row:
                jobs[str(row[0])] = {"gpu_wh": round(row[1] / 3.6e6, 3), "cpu_wh": round(row[2] / 3.6e6, 3),
                                     "gpu_metered_seconds": round(row[3], 1),
                                     "cpu_metered_seconds": round(row[4], 1)}
        day = dt.datetime.fromtimestamp(reading["time"]).astimezone().date().isoformat()
        row = self.db.execute("SELECT gpu_mj, cpu_mj, job_gpu_mj, job_cpu_mj FROM days WHERE day=?",
                              (day,)).fetchone() or (0, 0, 0, 0)
        return {"jobs": jobs, "today": {"day": day, "gpu_wh": round(row[0] / 3.6e6, 2),
                                        "cpu_package_wh": round(row[1] / 3.6e6, 2),
                                        "job_gpu_wh": round(row[2] / 3.6e6, 2),
                                        "job_cpu_wh": round(row[3] / 3.6e6, 2)},
                "cpu_measured": reading.get("rapl_uj") is not None}
