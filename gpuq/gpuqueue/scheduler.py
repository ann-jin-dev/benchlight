"""Scheduling policy, independent of CUDA, Docker, and systemd."""

from .store import ACTIVE, TERMINAL, now


def choose(store, config, snapshot):
    """Atomically reserve one job; FIFO among ready jobs, without backfilling.

    A ready two-GPU job at the front reserves the next turn for itself, so a
    stream of smaller jobs cannot continually occupy its second card.
    """
    with store.transaction():
        queued = sorted(store.jobs(("queued",)), key=lambda job: (-job["priority"], job["id"]))
        ready = []
        for job in queued:
            dependencies = [store.get(i) for i in store.dependencies(job["id"])]
            failed = [dep for dep in dependencies if dep["status"] in TERMINAL and
                      dep["status"] != "succeeded"]
            if failed:
                store.update(job["id"], status="skipped", finished_at=now(),
                             detail="Dependency did not succeed: " +
                             ", ".join(str(dep["id"]) for dep in failed))
                continue
            pending = [dep for dep in dependencies if dep["status"] != "succeeded"]
            if pending:
                store.update(job["id"], detail="Waiting for dependencies: " +
                             ", ".join(str(dep["id"]) for dep in pending))
                continue
            ready.append(job)

        if not ready:
            return None
        if store.setting("paused", False):
            for job in ready:
                store.update(job["id"], detail="Queue is paused")
            return None
        if snapshot.get("error"):
            for job in ready:
                store.update(job["id"], detail="Host check failed: " + snapshot["error"])
            return None

        active = store.jobs(ACTIVE)
        reserved = {gpu for job in active for gpu in job["allocation"]}
        cpus = config["max_cpus"] - sum(job["spec"]["cpus"] for job in active)
        memory = config["max_memory_bytes"] - sum(job["spec"]["memory_bytes"] for job in active)
        blocked = snapshot.get("blocked", {})
        available = [gpu for gpu in snapshot["gpus"] if gpu["uuid"] not in reserved and
                     gpu["uuid"] not in blocked]
        available.sort(key=lambda gpu: (gpu["memory_used_mib"], gpu["index"]))

        job = ready[0]
        spec = job["spec"]
        requested = spec.get("gpu_uuids", [])
        if requested:
            by_uuid = {gpu["uuid"]: gpu for gpu in available}
            available = [by_uuid[gpu] for gpu in requested if gpu in by_uuid]
        reasons = []
        if len(available) < spec["gpus"]:
            reasons.append(f"needs {spec['gpus']} GPU(s); {len(available)} available")
            affected = requested or [gpu["uuid"] for gpu in config["gpus"]]
            reasons.extend(blocked[gpu] for gpu in affected if gpu in blocked)
        if spec["cpus"] > cpus:
            reasons.append(f"needs {spec['cpus']} CPUs; {cpus} unreserved")
        if spec["memory_bytes"] > memory:
            reasons.append("RAM reservation budget is occupied")
        headroom = snapshot["memory_available_bytes"] - config["host_memory_headroom_bytes"]
        if spec["memory_bytes"] > headroom:
            reasons.append("host available RAM is below this job's request plus headroom")
        if reasons:
            store.update(job["id"], detail="Waiting: " + "; ".join(reasons))
            for following in ready[1:]:
                store.update(following["id"], detail=f"Waiting behind ready job {job['id']} (FIFO)")
            return None

        selected = available[:spec["gpus"]]
        allocation = [gpu["uuid"] for gpu in selected]
        instance = store.setting("instance")
        runtime = {"name": f"gpuq-{instance[:12]}-{job['id']}", "instance": instance,
                   "gpus": [{key: gpu[key] for key in ("index", "uuid", "name")} for gpu in selected]}
        store.update(job["id"], status="starting", allocation=allocation, runtime=runtime,
                     started_at=now(), detail="Starting")
        return store.get(job["id"])
