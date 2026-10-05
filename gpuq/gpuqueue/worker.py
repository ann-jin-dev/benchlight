"""The single scheduling worker. A restart leaves job supervisors running."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import signal
import sys
import time

from . import runtime as rt
from .scheduler import choose
from .store import ACTIVE, TERMINAL, Store, load_config, now


def finish(store, job, status, exit_code, detail, result=None):
    with store.transaction():
        current = store.get(job["id"])
        runtime = dict(current["runtime"], finalized=True)
        runtime["cleanup_done"] = not bool(runtime.get("name"))
        if result:
            runtime["result"] = result
        # A cancellation received before completion is recorded as cancellation.
        if current["status"] == "cancelling":
            if runtime.get("termination_reason") == "timeout":
                status, exit_code, detail = "failed", 124, "Job exceeded its time limit"
            else:
                status, detail = "cancelled", "Cancelled by user"
        store.update(job["id"], status=status, exit_code=exit_code,
                     finished_at=current["finished_at"] or now(), detail=detail, runtime=runtime)
    store.prepare_run(job["id"])
    store.manifest(job["id"])


def reconcile(store, job, config):
    job_id = job["id"]
    name = job["runtime"]["name"]
    backend = job["spec"]["backend"]
    inspect = rt.docker_inspect if backend == "docker" else rt.native_inspect
    external = inspect(name)
    if backend == "docker" and external is not None:
        rt.require_docker_owner(job, external)
    if backend == "native" and external is not None:
        rt.require_native_owner(job, external)
    limit = job["spec"].get("timeout_seconds")
    if limit and job["status"] != "cancelling" and rt.elapsed(job) > limit:
        with store.transaction():
            current = store.get(job_id)
            # Preserve an explicit user cancellation if it arrived first.
            if current["status"] != "cancelling":
                store.update(job_id, status="cancelling", detail="Stopping: time limit exceeded",
                             runtime=dict(current["runtime"], termination_reason="timeout"))
        job = store.get(job_id)

    if store.get(job_id)["status"] == "cancelling":
        if backend == "docker":
            rt.stop_docker(job)
        else:
            rt.stop_native(job)
        external = inspect(name)
        if external is None:
            finish(store, job, "cancelled", None, "Cancelled before start")
            return
        job = store.get(job_id)

    if external is None:
        if job["status"] == "starting":
            if (job["runtime"].get("external_confirmed") or
                    (job["runtime"].get("launch_attempted") and
                     job["runtime"].get("launch_boot_id") != rt.boot_id())):
                finish(store, job, "failed", None,
                       "Started or uncertain runtime disappeared; automatic replay is disabled")
                return
            if backend == "docker":
                rt.start_docker(store, job, config)
            else:
                rt.start_native(store, job, config)
            return
        finish(store, job, "failed", None, "Job runtime disappeared; automatic replay is disabled")
        return

    if backend == "docker":
        state = external["State"]
        if state["Status"] == "created" and store.get(job_id)["status"] == "starting":
            rt.start_docker(store, job, config)
            return
        if state.get("Running"):
            with store.transaction():
                if store.get(job_id)["status"] != "cancelling":
                    store.update(job_id, status="running", detail="Running")
            rt.collect_docker_logs(store, store.get(job_id))
            return
        if state["Status"] not in ("exited", "dead"):
            raise rt.HostError("Unexpected Docker state: " + state["Status"])
        rt.collect_docker_logs(store, store.get(job_id))
        code = state["ExitCode"]
        reason = "Completed" if code == 0 else f"Command exited with status {code}"
        if state.get("OOMKilled"):
            reason = "Container exceeded its RAM limit (OOM killed)"
        result = {key: state.get(key) for key in
                  ("Status", "ExitCode", "OOMKilled", "Error", "StartedAt", "FinishedAt")}
        finish(store, job, "succeeded" if code == 0 else "failed", code, reason, result)
    else:
        state, substate = external["ActiveState"], external["SubState"]
        if state in ("activating", "deactivating", "reloading") or substate == "running":
            with store.transaction():
                if store.get(job_id)["status"] != "cancelling":
                    store.update(job_id, status="running", detail="Running")
            return
        if substate == "exited" or state in ("inactive", "failed"):
            code = int(external.get("ExecMainStatus", 0))
            success = external.get("Result") == "success" and code == 0
            if external.get("Result") == "timeout":
                code, reason = 124, "Job exceeded its time limit"
            elif external.get("Result") == "oom-kill":
                reason = "Job exceeded its RAM limit (OOM killed)"
            else:
                reason = "Completed" if success else "Native service failed: " + external.get("Result", "unknown")
            finish(store, job, "succeeded" if success else "failed", code, reason, external)
            return
        raise rt.HostError("Unexpected native service state: " + str(external))


def sync_terminal(store):
    for job in store.jobs(TERMINAL):
        if not job["runtime"].get("finalized"):
            finish(store, job, job["status"], job["exit_code"], job["detail"])
            job = store.get(job["id"])
        if not job["runtime"].get("cleanup_done"):
            rt.cleanup(job)
            store.update(job["id"], runtime=dict(job["runtime"], cleanup_done=True))
            store.manifest(job["id"])


def tick(store, config):
    errors = []
    for job in store.jobs(ACTIVE):
        try:
            reconcile(store, job, config)
        except rt.JobError as error:
            finish(store, job, "failed", 127, str(error))
        except rt.HostError as error:
            message = str(error)
            errors.append(message)
            store.update(job["id"], detail="Runtime check pending: " + message)
    try:
        sync_terminal(store)
    except rt.HostError as error:
        errors.append(str(error))

    observation = rt.snapshot(config, store.jobs(ACTIVE))
    if errors:
        observation["error"] = "; ".join(errors + ([observation["error"]] if observation["error"] else []))
    store.set_setting("worker", {"pid": os.getpid(), "heartbeat": now(),
                                 "state": "degraded" if observation["error"] else "running",
                                 "snapshot": observation})
    # Reserve and launch jobs sequentially. Persist the reservation before starting.
    while True:
        job = choose(store, config, observation)
        if job is None:
            break
        store.prepare_run(job["id"])
        store.manifest(job["id"])
        try:
            if job["spec"]["backend"] == "docker":
                rt.start_docker(store, job, config)
            else:
                rt.start_native(store, job, config)
        except rt.JobError as error:
            finish(store, job, "failed", 127, str(error))
        except rt.HostError as error:
            store.update(job["id"], detail="Start pending: " + str(error))
            break
    sync_terminal(store)


def run(root: Path, once=False):
    os.umask(0o077)
    config = load_config(root)
    store = Store(root)
    with (store.root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("A scheduling worker already holds this queue's lock") from error
        # Independent state directories must not create competing schedulers for
        # the same user's hardware. All projects use one machine coordinator.
        machine_lock_path = Path.home() / ".local/state/gpuq-machine.lock"
        machine_lock_path.parent.mkdir(parents=True, exist_ok=True)
        machine_lock = machine_lock_path.open("a")
        try:
            fcntl.flock(machine_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            machine_lock.close()
            raise ValueError("Another GPU queue worker already coordinates this account's machine") from error
        stopping = False

        def stop(_signal, _frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print(f"gpuq worker ready: {store.root}", flush=True)
        try:
            while not stopping:
                started = time.monotonic()
                try:
                    tick(store, config)
                except rt.HostError as error:
                    print(f"Host check pending: {error}", file=sys.stderr, flush=True)
                    previous = store.setting("worker", {})
                    previous.update(heartbeat=now(), state="degraded", error=str(error))
                    store.set_setting("worker", previous)
                if once:
                    break
                # Short sleeps let systemd stop the worker promptly without touching jobs.
                deadline = started + config["poll_seconds"]
                while not stopping and time.monotonic() < deadline:
                    time.sleep(max(0, min(0.2, deadline - time.monotonic())))
        finally:
            previous = store.setting("worker", {})
            previous.update(heartbeat=now(), state="stopped")
            store.set_setting("worker", previous)
            store.close()
            machine_lock.close()
