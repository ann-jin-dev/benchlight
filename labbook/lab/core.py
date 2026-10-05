"""The lab notebook: receipts, run replay and the public window, from one place."""

from __future__ import annotations

import re
import time
from pathlib import Path

from .config import load_config
from .receipts import TERMINAL, build_receipt, summary_row
from .sources import CodexSource, GpuqSource, PulseSource, parse_time

PROTOCOL_MARKER = "\n---\n## Operating protocol"


def section(markdown: str, heading: str, limit: int = 900) -> str:
    """The body of one '## heading' section, or the start of the text."""
    match = re.search(rf"^##\s*{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", markdown, re.M | re.S)
    text = (match.group(1) if match else markdown).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


class Lab:
    def __init__(self, config: dict | None = None):
        self.config = config or load_config()
        self.gpuq = GpuqSource(self.config["gpuq_state_dir"])
        self.pulse = PulseSource(self.config["pulse_state_dir"])
        self.codex = CodexSource(self.config["codex_runs_dir"], self.config["codex_sessions_dir"])
        self._windows = (0.0, [])

    # ------------------------------------------------------------ agent links

    def turn_windows(self) -> list[dict]:
        """Every Codex turn with its time window, refreshed at most every 10 s.

        A turn without an end marker is open only while its runner is alive;
        otherwise (a reboot, a killed runner) it ends at its last recorded event.
        """
        stamp, windows = self._windows
        if time.time() - stamp < 10:
            return windows
        windows = []
        for run_id in self.codex.run_ids():
            meta = self.codex.meta(run_id) or {}
            for turn in self.codex.turns(run_id):
                end = turn["ended"]
                if end is None:
                    end = None if turn["runner_alive"] else (turn["last_activity"] or turn["started"])
                windows.append({"run": run_id, "turn": turn["n"], "cwd": meta.get("cwd") or "",
                                "started": turn["started"], "ended": end,
                                "submitted": [i for s in self.codex.activity(run_id, turn["n"])["submissions"]
                                              for i in s["job_ids"]]})
        self._windows = (time.time(), windows)
        return windows

    @staticmethod
    def _within(window: dict, moment: float | None, slack: float = 0) -> bool:
        if moment is None or not window["started"]:
            return False
        return window["started"] - slack <= moment <= (window["ended"] or time.time()) + slack

    def link(self, job: dict) -> dict | None:
        """Which agent asked for this job, and how sure that is.

        Exact evidence always wins over inference: the origin gpuq recorded at
        submission, then a job id printed by a `gpuq submit` inside a Codex turn
        (only if the job was created during that turn), and only then a turn
        whose time window and project folder contain the submission.
        """
        origin = job["spec"].get("origin") or {}
        if origin.get("codex_run"):
            turn = origin.get("codex_turn")
            return {"run": origin["codex_run"], "turn": int(turn) if str(turn).isdigit() else None,
                    "link": "exact", "evidence": "CODEX_TASK_RUN recorded by gpuq at submission"}
        if origin.get("claude_session"):
            return {"run": None, "turn": None, "link": "exact", "claude_session": origin["claude_session"][:8],
                    "evidence": "CLAUDE_CODE_SESSION_ID recorded by gpuq at submission"}
        windows = self.turn_windows()
        created = parse_time(job.get("created_at"))
        for window in windows:
            if job["id"] in window["submitted"] and self._within(window, created, slack=60):
                return {"run": window["run"], "turn": window["turn"], "link": "exact",
                        "evidence": "job id printed by a `gpuq submit` Codex ran in this turn"}
        cwd = job["spec"].get("original_cwd") or job["spec"].get("cwd") or ""
        for window in windows:
            if not window["cwd"]:
                continue
            inside = cwd == window["cwd"] or cwd.startswith(window["cwd"].rstrip("/") + "/")
            if inside and self._within(window, created):
                return {"run": window["run"], "turn": window["turn"], "link": "inferred",
                        "evidence": "submitted during this turn from inside its project folder"}
        return None

    def agent_detail(self, link: dict | None) -> dict | None:
        if not link or not link.get("run"):
            return link
        meta = self.codex.meta(link["run"]) or {}
        detail = dict(link, run_name=meta.get("name"), model=meta.get("model"), effort=meta.get("effort"))
        if link.get("turn"):
            turn = self.codex.turn(link["run"], link["turn"])
            detail["turn_status"] = turn["status"]
            detail["turn_tokens"] = self.codex.turn_tokens(meta.get("thread_id"), turn["turn_id"])
        notes = self.codex.notes(link["run"])
        detail["notes"] = [n for n in notes if not link.get("turn") or (n.get("turn") or 0) >= link["turn"]]
        return detail

    # ------------------------------------------------------------ receipts

    def receipt(self, job_id: int) -> dict | None:
        job = self.gpuq.job(job_id)
        if job is None:
            return None
        instance = job["runtime"].get("instance") or self.gpuq.instance()
        return build_receipt(
            job, instance=instance, energy=self.pulse.job_energy(instance, job_id),
            agent=self.agent_detail(self.link(job)), snapshot=self.gpuq.snapshot_summary(job["spec"]),
            artifacts=self.gpuq.artifacts(job_id), electricity=self.config["electricity"])

    def receipts(self, limit: int = 200, project: str | None = None) -> list[dict]:
        instance = self.gpuq.instance()
        energy = self.pulse.energy_by_job(instance)
        return [summary_row(job, energy.get(job["id"]), self.link(job))
                for job in self.gpuq.jobs(limit=limit, project=project)]

    # ------------------------------------------------------------ run replay

    def runs(self) -> list[dict]:
        rows = []
        for run_id in self.codex.run_ids():
            meta = self.codex.meta(run_id) or {}
            turns = self.codex.turns(run_id)
            notes = self.codex.notes(run_id)
            last = turns[-1] if turns else None
            rows.append({"id": run_id, "name": meta.get("name"), "project": Path(meta.get("cwd") or "").name,
                         "created": turns[0]["started"] if turns else None, "turns": len(turns),
                         "state": "running" if last and not last["ended"] and last["runner_alive"] else "ended",
                         "status": last["status"] if last else None, "wakes": meta.get("wakes", 0),
                         "notes": len(notes),
                         "verdicts_decisions": sum(n.get("kind") in ("verdict", "decision") for n in notes)})
        return rows

    def run_jobs(self, run_id: str) -> list[dict]:
        jobs = []
        for job in self.gpuq.jobs():
            link = self.link(job)
            if link and link.get("run") == run_id:
                jobs.append((job, link))
        return jobs

    def timeline(self, run_id: str) -> dict | None:
        meta = self.codex.meta(run_id)
        if meta is None:
            return None
        turns = self.codex.turns(run_id)
        events = []
        tokens_total = 0
        for turn in turns:
            prompt = turn["prompt"].split(PROTOCOL_MARKER)[0].strip()
            events.append({"at": turn["started"], "actor": "claude", "turn": turn["n"],
                           "kind": "brief" if turn["n"] == 1 else "instruction",
                           "title": "Brief" if turn["n"] == 1 else f"Instruction for turn {turn['n']}",
                           "text": prompt[:1200] + ("…" if len(prompt) > 1200 else "")})
            for steer in turn["steers"]:
                events.append({"at": steer["at"], "actor": "claude", "turn": turn["n"], "kind": "steer",
                               "title": "Steered the running turn", "text": steer["text"][:600]})
            if turn["ended"]:
                activity = self.codex.activity(run_id, turn["n"])
                tokens = self.codex.turn_tokens(meta.get("thread_id"), turn["turn_id"])
                tokens_total += (tokens or {}).get("total_tokens", 0)
                events.append({"at": turn["ended"], "actor": "codex", "turn": turn["n"], "kind": "report",
                               "title": f"Turn {turn['n']} report", "status": turn["status"],
                               "text": section(turn["final"], "Summary"),
                               "duration_seconds": round(turn["ended"] - turn["started"]) if turn["started"] else None,
                               "activity": {k: activity[k] for k in ("commands", "edits", "messages")},
                               "tokens": tokens})
            elif turn["started"] and turn["runner_alive"]:
                events.append({"at": time.time(), "actor": "codex", "turn": turn["n"], "kind": "working",
                               "title": f"Turn {turn['n']} in progress",
                               "activity": {k: v for k, v in self.codex.activity(run_id, turn["n"]).items()
                                            if k != "submissions"}})
        instance = self.gpuq.instance()
        energy = self.pulse.energy_by_job(instance)
        gpu_seconds = energy_wh = 0.0
        statuses: dict[str, int] = {}
        for job, link in self.run_jobs(run_id):
            started, finished = parse_time(job.get("started_at")), parse_time(job.get("finished_at"))
            statuses[job["status"]] = statuses.get(job["status"], 0) + 1
            if started:
                gpu_seconds += ((finished or time.time()) - started) * job["spec"].get("gpus", 0)
            record = energy.get(job["id"])
            wh = round((record["gpu_mj"] + record["cpu_mj"]) / 3.6e6, 2) if record else None
            energy_wh += wh or 0
            base = {"job": job["id"], "turn": link.get("turn"), "link": link["link"], "name": job["name"],
                    "gpus": job["spec"].get("gpus", 0)}
            events.append(dict(base, at=parse_time(job["created_at"]), actor="gpu", kind="job",
                               title=f"Job {job['id']} queued", text=job["name"]))
            if job["status"] in TERMINAL and finished:
                events.append(dict(base, at=finished, actor="gpu", kind="job-end", status=job["status"],
                                   title=f"Job {job['id']} {job['status']}", energy_wh=wh,
                                   duration_seconds=round(finished - started) if started else None))
        for wake in self.codex.wakes(run_id):
            events.append({"at": wake["at"], "actor": "system", "kind": "wake",
                           "title": "Unattended wake", "text": wake["text"]})
        notes = self.codex.notes(run_id)
        for note in notes:
            events.append({"at": note.get("at_epoch"), "actor": note.get("by", "claude"),
                           "kind": note.get("kind", "note"), "turn": note.get("turn"),
                           "title": {"verdict": "Verification verdict", "decision": "Decision"}.get(
                               note.get("kind"), "Note"), "text": note.get("text", "")})
        events = sorted((e for e in events if e.get("at")), key=lambda e: e["at"])
        first = events[0]["at"] if events else None
        last = events[-1]["at"] if events else None
        return {"run": {"id": run_id, "name": meta.get("name"), "project": Path(meta.get("cwd") or "").name,
                        "model": meta.get("model"), "effort": meta.get("effort"), "wakes": meta.get("wakes", 0),
                        "max_wakes": meta.get("max_wakes")},
                "totals": {"wall_seconds": round(last - first) if first and last else None,
                           "turns": len(turns), "jobs": statuses, "gpu_hours": round(gpu_seconds / 3600, 2),
                           "energy_wh": round(energy_wh, 1), "tokens": tokens_total,
                           "verdicts": sum(n.get("kind") == "verdict" for n in notes),
                           "decisions": sum(n.get("kind") == "decision" for n in notes)},
                "events": events}

    def turn_text(self, run_id: str, n: int, which: str) -> str | None:
        if self.codex.meta(run_id) is None:
            return None
        turn = self.codex.turn(run_id, n)
        return turn["prompt"] if which == "prompt" else turn["final"]
