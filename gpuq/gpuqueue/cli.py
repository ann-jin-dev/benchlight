"""Local CLI. Submission and status need neither the GPU nor Docker socket."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import time

from . import __version__, source
from .store import ACTIVE, TERMINAL, Store, default_root, load_config, now


def size(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?(?:i?b)?)?", str(value).lower())
    if not match:
        raise ValueError(f"Invalid memory size: {value!r}; use e.g. 8g or 512m")
    suffix = (match[2] or "").rstrip("b").rstrip("i")
    return int(float(match[1]) * 1024 ** (" kmgt".index(suffix) if suffix else 0))


def duration(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", str(value).lower())
    if not match:
        raise ValueError(f"Invalid duration: {value!r}; use e.g. 30m or 2h")
    return float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


# Set by agent wrappers so each job records which agent run asked for it.
ORIGIN_VARIABLES = {"CODEX_TASK_RUN": "codex_run", "CODEX_TASK_TURN": "codex_turn",
                    "CLAUDE_CODE_SESSION_ID": "claude_session"}


def parse_labels(values):
    labels = {}
    for item in values or []:
        key, separator, value = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", key) or len(value) > 256:
            raise ValueError("Labels use KEY=VALUE with a key of letters, digits, '.', '_' or '-' "
                             "and a value of at most 256 characters")
        labels[key] = value
    if len(labels) > 32:
        raise ValueError("At most 32 labels per job")
    return labels


def submission_origin():
    return {name: os.environ[variable][:128] for variable, name in ORIGIN_VARIABLES.items()
            if os.environ.get(variable)}


def dependency_ids(values):
    return sorted({int(item) for value in values or [] for item in value.split(",")})


def parse_mount(value):
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("Mounts use HOST_PATH:CONTAINER_PATH[:ro|rw]")
    host, target = parts[:2]
    mode = parts[2] if len(parts) == 3 else "ro"
    path = Path(host).expanduser().resolve()
    if not path.exists():
        raise ValueError("Mount source does not exist: " + str(path))
    if not target.startswith("/") or "," in target or mode not in ("ro", "rw"):
        raise ValueError("Mount targets must be absolute and mode must be ro or rw")
    reserved = ("/workspace/code", "/workspace/run", "/workspace/cache")
    if any(target == prefix or target.startswith(prefix + "/") or
           prefix.startswith(target.rstrip("/") + "/") for prefix in reserved):
        raise ValueError("Use --cwd or --cache-dir for the queue's reserved code/run/cache mounts")
    return {"source": str(path), "target": target, "mode": mode}


def submit(args, store, config):
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("Supply a foreground command after --")
    cwd = Path(args.cwd or os.getcwd()).expanduser().resolve()
    if not cwd.is_dir() or ":" in str(cwd):
        raise ValueError("Working directory must exist and its path must not contain ':'")
    project = args.project or cwd.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project):
        raise ValueError("Project names use letters, digits, dots, underscores and hyphens")
    if args.gpus < 0 or args.gpus > len(config["gpus"]):
        raise ValueError(f"--gpus must be between 0 and {len(config['gpus'])}")
    cpus = args.cpus if args.cpus is not None else 4 * max(1, args.gpus)
    memory = size(args.memory or f"{8 * max(1, args.gpus)}g")
    if not 1 <= cpus <= config["max_cpus"]:
        raise ValueError(f"--cpus must be between 1 and {config['max_cpus']}")
    if not 6 * 1024**2 <= memory <= config["max_memory_bytes"]:
        raise ValueError(f"RAM must be between 6 MiB and {config['max_memory_bytes'] / 1024**3:g} GiB")
    requested = []
    observed = store.setting("worker", {}).get("snapshot", {}).get("gpus") or config["gpus"]
    mapping = {str(gpu["index"]): gpu["uuid"] for gpu in observed}
    mapping.update({gpu["uuid"]: gpu["uuid"] for gpu in config["gpus"]})
    if args.gpu:
        try:
            requested = [mapping[token.strip()] for token in args.gpu.split(",")]
        except KeyError as error:
            raise ValueError(f"Unknown GPU: {error.args[0]}") from error
        if len(set(requested)) != args.gpus or len(requested) != args.gpus:
            raise ValueError("--gpu must name exactly --gpus distinct devices")
    environment = {}
    for item in args.env:
        key, separator, value = item.partition("=")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("Invalid environment variable name: " + key)
        if key.startswith("GPUQ_") or key in ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "NVIDIA_VISIBLE_DEVICES"):
            raise ValueError("The queue controls GPU assignments and GPUQ_* environment variables")
        if not separator:
            if key not in os.environ:
                raise ValueError(f"Environment variable {key} is not set in this shell")
            value = os.environ[key]
        environment[key] = value
    env_files = [str(Path(path).expanduser().resolve()) for path in args.env_file]
    if any(not Path(path).is_file() for path in env_files):
        raise ValueError("All --env-file paths must exist")
    timeout = duration(args.timeout) if args.timeout else None
    if timeout is not None and timeout <= 0:
        raise ValueError("Time limits must be positive")
    if not args.image and (args.mount or args.entrypoint is not None or args.cache_dir):
        raise ValueError("--mount, --entrypoint and --cache-dir need --image")
    if not args.image:
        if "/" in command[0]:
            executable = Path(command[0]).expanduser()
            if not executable.is_absolute():
                executable = cwd / executable
        else:
            executable = shutil.which(command[0])
        if executable:
            # Python locates a virtual environment from its invocation path.
            # Make that path absolute without resolving its symlink target.
            executable = str(Path(executable).absolute())
        if not executable or not os.access(executable, os.X_OK):
            raise ValueError(f"Native command is not executable: {command[0]}")
        command = [executable, *command[1:]]
        # Preserve an activated environment's PATH, without copying credentials.
        environment.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    shm = size(args.shm_size)
    if args.image and not 1024**2 <= shm <= memory:
        raise ValueError("--shm-size must be at least 1 MiB and no larger than --memory")
    if not args.container_cwd.startswith("/"):
        raise ValueError("--container-cwd must be absolute")
    dependencies = dependency_ids(args.after)
    for dependency in dependencies:
        if store.get(dependency) is None:
            raise ValueError(f"Dependency {dependency} does not exist")
    spec = {"project": project, "name": args.name or Path(command[0]).name,
            "backend": "docker" if args.image else "native", "image": args.image,
            "command": command, "cwd": str(cwd), "original_cwd": str(cwd),
            "container_cwd": args.container_cwd, "gpus": args.gpus, "gpu_uuids": requested,
            "cpus": cpus, "memory_bytes": memory, "shm_bytes": shm, "priority": args.priority,
            "env": environment, "env_files": env_files, "timeout_seconds": timeout,
            "mounts": [parse_mount(mount) for mount in args.mount], "entrypoint": args.entrypoint,
            "cache_dir": str(Path(args.cache_dir).expanduser().resolve()) if args.cache_dir else None,
            "git_at_submission": source.git_metadata(cwd),
            "labels": parse_labels(args.label), "origin": submission_origin()}
    snapshot = source.freeze(cwd, store.root) if args.snapshot else None
    if snapshot:
        spec["cwd"], spec["source_snapshot"] = str(snapshot), str(snapshot / ".gpuq-source.json")
        if not args.image and Path(command[0]).is_relative_to(cwd):
            snapshot_executable = snapshot / Path(command[0]).relative_to(cwd)
            if snapshot_executable.is_file():
                spec["command"] = [str(snapshot_executable), *command[1:]]
    try:
        job_id = store.enqueue(spec, dependencies)
    except BaseException:
        if snapshot:
            shutil.rmtree(snapshot)
        raise
    store.prepare_run(job_id)
    store.manifest(job_id)
    print(job_id)


def status(args, store, config):
    worker = store.setting("worker", {})
    observation = worker.get("snapshot", {})
    age = None
    if worker.get("heartbeat"):
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(worker["heartbeat"])).total_seconds()
    state = worker.get("state", "not started")
    if age is not None and age > max(15, config["poll_seconds"] * 5):
        state = "unresponsive (last heartbeat is stale)"
    global_jobs = store.jobs()
    all_jobs = global_jobs
    if args.project:
        all_jobs = [job for job in all_jobs if job["project"] == args.project]
    jobs = all_jobs if args.all else [job for job in all_jobs if job["status"] not in TERMINAL]
    counts = dict(collections.Counter(job["status"] for job in jobs))
    reservations = {gpu: job["id"] for job in global_jobs if job["status"] in ACTIVE
                    for gpu in job["allocation"]}
    if args.json:
        for job in jobs:
            job["spec"]["env"] = {key: "<set>" for key in job["spec"].get("env", {})}
            job["run_dir"] = str(store.run_dir(job["id"]))
        print(json.dumps({"worker_state": state, "heartbeat_age_seconds": age,
                          "paused": store.setting("paused", False), "counts": counts,
                          "reservations": reservations,
                          "budgets": {"cpus": config["max_cpus"], "memory_bytes": config["max_memory_bytes"]},
                          "snapshot": observation, "jobs": jobs}, indent=2))
        return
    print(f"Worker: {state} | Dispatch: {'paused' if store.setting('paused', False) else 'enabled'}")
    print(f"Budget: {config['max_cpus']} CPUs, {config['max_memory_bytes'] / 1024**3:g} GiB RAM")
    if observation.get("error"):
        print("Host check pending: " + observation["error"])
    unreserved = "available" if state == "running" and not observation.get("error") else "unverified"
    for gpu in observation.get("gpus") or config["gpus"]:
        assignment = (f"job {reservations[gpu['uuid']]}" if gpu["uuid"] in reservations
                      else observation.get("blocked", {}).get(gpu["uuid"], unreserved))
        print(f"GPU {gpu['index']}: {gpu['name']} | {assignment}")
    print("Jobs: " + (", ".join(f"{count} {state}" for state, count in sorted(counts.items())) or "none"))
    if jobs:
        print(f"{'ID':>6}  {'STATE':<11} {'PROJECT':<20} {'GPUS':>4} {'CPUS':>4} {'RAM':>6}  NAME / DETAIL")
        for job in jobs[-args.limit:]:
            spec = job["spec"]
            print(f"{job['id']:>6}  {job['status']:<11} {job['project'][:20]:<20} "
                  f"{spec['gpus']:>4} {spec['cpus']:>4} {spec['memory_bytes'] / 1024**3:>5.1f}G  "
                  f"{job['name']} / {job['detail']}")
    elif not args.all and all_jobs:
        print("No pending jobs. Use gpuq status --all for history.")


def last_lines(path, count):
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        end = position
        if count <= 0:
            return [], end
        chunks, newlines = [], 0
        while position > 0 and newlines <= count:
            length = min(position, 8192)
            position -= length
            stream.seek(position)
            chunk = stream.read(length)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
        lines = b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines(keepends=True)[-count:]
        return lines, end


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="gpuq", description="Persistent GPU queue for this workstation")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--state-dir", default=str(default_root()))
    commands = parser.add_subparsers(dest="action", required=True)
    submit_parser = commands.add_parser("submit", help="Queue a Docker image or native foreground command")
    submit_parser.add_argument("--cwd")
    submit_parser.add_argument("--project")
    submit_parser.add_argument("--name")
    submit_parser.add_argument("--image", help="Existing Docker image; omission runs a native user service")
    submit_parser.add_argument("--gpus", type=int, default=1)
    submit_parser.add_argument("--gpu", help="Optional physical indices or UUIDs, comma-separated")
    submit_parser.add_argument("--cpus", type=int)
    submit_parser.add_argument("--memory", help="Hard RAM limit, default 8g per GPU (8g for CPU jobs)")
    submit_parser.add_argument("--shm-size", default="2g")
    submit_parser.add_argument("--mount", action="append", default=[])
    submit_parser.add_argument("--env", action="append", default=[])
    submit_parser.add_argument("--env-file", action="append", default=[])
    submit_parser.add_argument("--cache-dir")
    submit_parser.add_argument("--container-cwd", default="/workspace/code")
    submit_parser.add_argument("--entrypoint")
    submit_parser.add_argument("--after", action="append", default=[])
    submit_parser.add_argument("--priority", type=int, default=0, help="Larger values run first, then FIFO")
    submit_parser.add_argument("--timeout", help="Optional job time limit, e.g. 2h")
    submit_parser.add_argument("--snapshot", action="store_true", help="Freeze Git source before enqueueing (256 MiB cap)")
    submit_parser.add_argument("--label", action="append", default=[],
                               help="KEY=VALUE metadata kept with the job, e.g. seed=3 or experiment=ablation")
    submit_parser.add_argument("command", nargs=argparse.REMAINDER)
    status_parser = commands.add_parser("status", help="Show queue and cached host telemetry")
    status_parser.add_argument("--all", action="store_true")
    status_parser.add_argument("--project")
    status_parser.add_argument("--limit", type=int, default=50)
    status_parser.add_argument("--json", action="store_true")
    for name in ("show", "logs"):
        command = commands.add_parser(name)
        command.add_argument("job_id", type=int)
        if name == "logs":
            command.add_argument("-n", "--lines", type=int, default=50)
            command.add_argument("-f", "--follow", action="store_true")
    cancel = commands.add_parser("cancel", help="Cancel queued jobs or stop only their job runtimes")
    cancel.add_argument("job_ids", type=int, nargs="+")
    retry = commands.add_parser("retry", help="Create a new attempt and preserve the old attempt")
    retry.add_argument("job_id", type=int)
    retry.add_argument("--after", action="append")
    wait = commands.add_parser("wait", help="Wait until jobs finish; fails if any job does not succeed")
    wait.add_argument("job_ids", type=int, nargs="+")
    wait.add_argument("--timeout")
    commands.add_parser("pause", help="Pause new starts; running jobs continue")
    commands.add_parser("resume", help="Resume dispatching queued jobs")
    commands.add_parser("doctor", help="Perform read-only host checks (requires host access)")
    worker_parser = commands.add_parser("worker", help="Run the scheduling service (normally managed by systemd)")
    worker_parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.state_dir).expanduser().resolve()
    try:
        config = load_config(root)
        if args.action == "worker":
            from .worker import run
            run(root, args.once)
            return 0
        store = Store(root, read_only=args.action in ("status", "show", "logs", "wait", "doctor"))
        try:
            if args.action == "submit":
                submit(args, store, config)
            elif args.action == "status":
                if args.limit < 1:
                    raise ValueError("--limit must be positive")
                status(args, store, config)
            elif args.action in ("pause", "resume"):
                store.set_setting("paused", args.action == "pause")
                print("New starts paused; running jobs continue" if args.action == "pause" else "Dispatch enabled")
            elif args.action == "cancel":
                with store.transaction():
                    jobs = [store.get(job_id) for job_id in args.job_ids]
                    if any(job is None for job in jobs):
                        raise ValueError("At least one job ID does not exist")
                    for job in jobs:
                        if job["status"] == "queued":
                            store.update(job["id"], status="cancelled", finished_at=now(), detail="Cancelled before start")
                        elif job["status"] in ACTIVE:
                            store.update(job["id"], status="cancelling", detail="Cancellation requested")
                for job in jobs:
                    store.manifest(job["id"])
                    print(f"{job['id']}: {store.get(job['id'])['status']}")
            elif args.action == "retry":
                job = store.get(args.job_id)
                if job is None or job["status"] not in TERMINAL:
                    raise ValueError("Retry needs an existing finished job")
                spec = dict(job["spec"])
                # A retry belongs to whoever asks for it, not to the original submitter.
                spec["origin"] = submission_origin()
                if job["runtime"].get("image_id"):
                    spec["image"] = job["runtime"]["image_id"]
                dependencies = (store.dependencies(args.job_id) if args.after is None
                                else dependency_ids(args.after))
                job_id = store.enqueue(spec, dependencies, retry_of=args.job_id)
                store.prepare_run(job_id)
                store.manifest(job_id)
                print(job_id)
            elif args.action in ("show", "logs"):
                job = store.get(args.job_id)
                if job is None:
                    raise ValueError("Job does not exist")
                if args.action == "show":
                    job["spec"]["env"] = {key: "<set>" for key in job["spec"].get("env", {})}
                    job["run_dir"] = str(store.run_dir(job["id"]))
                    job["dependencies"] = store.dependencies(job["id"])
                    print(json.dumps(job, indent=2))
                else:
                    path = store.run_dir(job["id"]) / "console.log"
                    lines, offset = last_lines(path, args.lines)
                    for line in lines:
                        sys.stdout.write(line)
                    if args.follow:
                        with path.open("r", errors="replace") as stream:
                            stream.seek(offset)
                            while True:
                                data = stream.read()
                                if data:
                                    print(data, end="", flush=True)
                                if store.get(job["id"])["status"] in TERMINAL:
                                    print(stream.read(), end="", flush=True)
                                    break
                                time.sleep(0.5)
            elif args.action == "wait":
                timeout = duration(args.timeout) if args.timeout else None
                deadline = time.monotonic() + timeout if timeout is not None else None
                while True:
                    jobs = [store.get(job_id) for job_id in args.job_ids]
                    if any(job is None for job in jobs):
                        raise ValueError("At least one job ID does not exist")
                    if all(job["status"] in TERMINAL for job in jobs):
                        for job in jobs:
                            print(f"{job['id']}: {job['status']} (exit {job['exit_code']})")
                        return 0 if all(job["status"] == "succeeded" for job in jobs) else 1
                    if deadline is not None and time.monotonic() >= deadline:
                        print("Wait timed out; jobs continue in the queue", file=sys.stderr)
                        return 124
                    time.sleep(0.5)
            elif args.action == "doctor":
                from .runtime import snapshot
                observation = snapshot(config, store.jobs(ACTIVE))
                print(json.dumps(observation, indent=2))
                return 1 if observation["error"] else 0
        finally:
            store.close()
    except (ValueError, OSError) as error:
        print(f"gpuq: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0
