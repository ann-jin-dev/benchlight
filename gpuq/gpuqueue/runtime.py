"""Host observations and independently supervised job runtimes.

Docker containers and transient systemd user services outlive the queue worker.
Their deterministic names make starting and recovering a job idempotent.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess


class HostError(RuntimeError):
    """Host state is uncertain: keep reservations and stop dispatching."""


class JobError(RuntimeError):
    """A confirmed job input/start failure, before an external job exists."""


def call(argv, *, timeout=15):
    try:
        return subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HostError(f"{argv[0]}: {error}") from error


def checked(argv, *, timeout=15):
    result = call(argv, timeout=timeout)
    if result.returncode:
        raise HostError((result.stderr or result.stdout or f"{argv[0]} failed").strip()[:1000])
    return result.stdout


def systemctl(*arguments):
    return checked(["systemctl", "--user", *arguments])


def gpu_info():
    output = checked(["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,driver_version",
                      "--format=csv,noheader,nounits"])
    try:
        return [{"index": int(row[0]), "uuid": row[1].strip(), "name": row[2].strip(),
                 "memory_total_mib": int(row[3]), "memory_used_mib": int(row[4]),
                 "utilization_percent": int(row[5]), "driver": row[6].strip()}
                for row in csv.reader(output.splitlines()) if row]
    except (ValueError, IndexError) as error:
        raise HostError("Cannot parse NVIDIA GPU telemetry") from error


def docker_inspect(name):
    result = call(["docker", "inspect", "--type", "container", name])
    if result.returncode:
        if "No such container" in result.stderr or "No such object" in result.stderr:
            return None
        raise HostError(result.stderr.strip()[:1000])
    try:
        return json.loads(result.stdout)[0]
    except (ValueError, IndexError) as error:
        raise HostError("Invalid Docker inspection response") from error


def native_inspect(name):
    output = systemctl("show", name + ".service", "--no-pager",
                       "--property=LoadState,ActiveState,SubState,ExecMainStatus,ExecMainCode,Result,MainPID,Description")
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if values.get("LoadState") == "not-found":
        return None
    if values.get("LoadState") != "loaded":
        raise HostError("Cannot inspect native unit: " + str(values))
    return values


def require_docker_owner(job, container):
    labels = container["Config"].get("Labels") or {}
    if (labels.get("io.gpuq.instance") != job["runtime"]["instance"] or
            labels.get("io.gpuq.job") != str(job["id"])):
        raise HostError("Refusing to manage a container with mismatched queue ownership")


def require_native_owner(job, unit):
    expected = f"GPUQ {job['runtime']['instance']} job {job['id']}"
    if unit.get("Description") != expected:
        raise HostError("Refusing to manage a native unit with mismatched queue ownership")


def memory_available():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise HostError("Host available RAM is unavailable")


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def record_launch_intent(store, job):
    with store.transaction():
        current = store.get(job["id"])
        if current["status"] == "cancelling":
            return False
        store.update(job["id"], runtime=dict(current["runtime"], launch_attempted=True,
                                            launch_boot_id=boot_id()))
    return True


def record_external_confirmed(store, job):
    with store.transaction():
        current = store.get(job["id"])
        store.update(job["id"], runtime=dict(current["runtime"], external_confirmed=True))


def requested_by_container(container, gpus):
    """Find GPU access even for a container that is currently sleeping."""
    known = {gpu["uuid"] for gpu in gpus}
    indices = {str(gpu["index"]): gpu["uuid"] for gpu in gpus}
    devices = set()
    requests = container["HostConfig"].get("DeviceRequests") or []
    for request in requests:
        capabilities = {item for group in request.get("Capabilities", []) for item in group}
        if request.get("Driver") != "nvidia" and "gpu" not in capabilities:
            continue
        ids = request.get("DeviceIDs") or []
        if not ids:
            devices.update(known)
        for token in ids:
            if token in known:
                devices.add(token)
            elif token in indices:
                devices.add(indices[token])
            else:
                # An unrecognized access request cannot be safely assigned to one card.
                devices.update(known)
    if not requests and container["HostConfig"].get("Runtime") == "nvidia":
        environment = dict(item.split("=", 1) for item in container["Config"].get("Env", []) if "=" in item)
        visible = environment.get("NVIDIA_VISIBLE_DEVICES", "all")
        if visible == "all":
            devices.update(known)
        elif visible not in ("", "void", "none"):
            for token in visible.split(","):
                devices.add(indices.get(token, token))
    # Older --device based GPU launchers do not necessarily use DeviceRequests.
    if any("nvidia" in device.get("PathOnHost", "")
           for device in container["HostConfig"].get("Devices") or []):
        devices.update(known)
    return devices


def snapshot(config, active):
    result = {"gpus": [], "blocked": {}, "memory_available_bytes": 0, "error": None}
    try:
        all_gpus = gpu_info()
        expected = {gpu["uuid"] for gpu in config["gpus"]}
        found = {gpu["uuid"] for gpu in all_gpus}
        if not expected <= found:
            raise HostError("Configured GPU UUID(s) missing: " + ", ".join(sorted(expected - found)))
        result["gpus"] = [gpu for gpu in all_gpus if gpu["uuid"] in expected]
        result["memory_available_bytes"] = memory_available()
        reserved = {gpu for job in active for gpu in job["allocation"]}
        owned = {job["runtime"]["name"] for job in active if job["spec"]["backend"] == "docker"}

        processes = checked(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                             "--format=csv,noheader,nounits"])
        for row in csv.reader(processes.splitlines()):
            if not row:
                continue
            if len(row) < 3:
                raise HostError("Cannot parse NVIDIA process telemetry")
            gpu, pid, name = (item.strip() for item in row[:3])
            if gpu not in expected or gpu in reserved:
                continue
            if Path(name).name in config["allowed_display_processes"]:
                continue
            result["blocked"][gpu] = f"GPU has an unmanaged process: {name} (PID {pid})"

        ids = checked(["docker", "ps", "--quiet"]).split()
        if ids:
            containers = json.loads(checked(["docker", "inspect", *ids]))
            for container in containers:
                name = container["Name"].lstrip("/")
                if name in owned:
                    continue
                for gpu in requested_by_container(container, result["gpus"]):
                    if gpu in expected and gpu not in reserved:
                        result["blocked"][gpu] = f"GPU is exposed to unmanaged container {name}"

        baselines = {gpu["uuid"]: gpu["idle_memory_mib"] for gpu in config["gpus"]}
        for gpu in result["gpus"]:
            if gpu["uuid"] in reserved or gpu["uuid"] in result["blocked"]:
                continue
            if gpu["memory_used_mib"] > baselines[gpu["uuid"]] + config["unmanaged_memory_margin_mib"]:
                result["blocked"][gpu["uuid"]] = "GPU memory exceeds its idle baseline"
            elif gpu["utilization_percent"] > config["unmanaged_utilization_percent"]:
                result["blocked"][gpu["uuid"]] = "GPU is busy outside the queue"
        # Native jobs require the same user manager used to supervise the worker.
        systemctl("show", "--property=Version", "--no-pager")
    except (HostError, ValueError, KeyError, OSError) as error:
        result["error"] = str(error)
    return result


def environment(job, config, run):
    spec = job["spec"]
    docker = spec["backend"] == "docker"
    indices = {gpu["uuid"]: str(gpu["index"]) for gpu in
               job["runtime"].get("gpus", config["gpus"])}
    values = {
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": str(spec["cpus"]), "MKL_NUM_THREADS": str(spec["cpus"]),
        "OPENBLAS_NUM_THREADS": str(spec["cpus"]), "NUMEXPR_NUM_THREADS": str(spec["cpus"]),
        "GPUQ_JOB_ID": str(job["id"]), "GPUQ_PROJECT": spec["project"],
        "GPUQ_RUN_DIR": "/workspace/run" if docker else str(run),
        "GPUQ_OUTPUT_DIR": "/workspace/run/outputs" if docker else str(run / "outputs"),
        "GPUQ_GPU_UUIDS": ",".join(job["allocation"]),
        "GPUQ_GPU_INDICES": ",".join(indices[gpu] for gpu in job["allocation"]),
        "TMPDIR": "/workspace/run/tmp" if docker else str(run / "tmp"),
    }
    if docker:
        values.update(HOME="/workspace/cache", XDG_CACHE_HOME="/workspace/cache",
                      HF_HOME="/workspace/cache/huggingface", TORCH_HOME="/workspace/cache/torch")
    for filename in spec.get("env_files", []):
        try:
            for line in Path(filename).read_text().splitlines():
                if line and not line.lstrip().startswith("#"):
                    key, separator, value = line.partition("=")
                    if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                        raise JobError(f"Invalid NAME=VALUE line in {filename}")
                    if key.startswith("GPUQ_") or key in ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "NVIDIA_VISIBLE_DEVICES"):
                        raise JobError(f"Environment file cannot override queue-controlled variable {key}")
                    values[key] = value
        except OSError as error:
            raise JobError(f"Cannot read environment file {filename}: {error}") from error
    values.update(spec.get("env", {}))
    # Device assignments are controlled by the queue, including CPU-only jobs.
    values["CUDA_VISIBLE_DEVICES"] = ",".join(job["allocation"])
    values["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    if docker:
        values["NVIDIA_VISIBLE_DEVICES"] = ",".join(job["allocation"]) or "void"
        values["NVIDIA_DRIVER_CAPABILITIES"] = "compute,utility"
    return values


def image_identity(image):
    result = call(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    if result.returncode:
        if "No such image" in result.stderr or "No such object" in result.stderr:
            raise JobError(f"Docker image {image!r} is not installed. Build or pull it before submitting.")
        raise HostError(result.stderr.strip()[:1000])
    return result.stdout.strip()


def start_docker(store, job, config):
    name = job["runtime"]["name"]
    existing = docker_inspect(name)
    if existing is not None:
        require_docker_owner(job, existing)
    if existing is None:
        spec = job["spec"]
        run = store.prepare_run(job["id"])
        if not Path(spec["cwd"]).is_dir():
            raise JobError(f"Working directory is missing: {spec['cwd']}")
        identity = job["runtime"].get("image_id") or image_identity(spec["image"])
        runtime = dict(job["runtime"], image_id=identity)
        store.update(job["id"], runtime=runtime)
        job = store.get(job["id"])
        store.manifest(job["id"])
        cache = Path(spec.get("cache_dir") or store.root / "cache" /
                     re.sub(r"[^A-Za-z0-9_.-]", "_", spec["project"]))
        cache.mkdir(parents=True, exist_ok=True)
        argv = ["docker", "create", "--name", name, "--init",
                "--label", "io.gpuq.instance=" + job["runtime"]["instance"],
                "--label", "io.gpuq.job=" + str(job["id"]),
                "--label", "io.gpuq.project=" + spec["project"],
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--cpus", str(spec["cpus"]), "--memory", str(spec["memory_bytes"]),
                "--memory-swap", str(spec["memory_bytes"]),
                "--shm-size", str(spec["shm_bytes"]),
                "--log-driver", "json-file", "--log-opt", "max-size=100m", "--log-opt", "max-file=3",
                "--workdir", spec["container_cwd"],
                "--volume", spec["cwd"] + ":/workspace/code:ro",
                "--volume", str(run) + ":/workspace/run:rw",
                "--volume", str(cache) + ":/workspace/cache:rw"]
        if job["allocation"]:
            argv.extend(["--gpus", '"device=' + ",".join(job["allocation"]) + '"'])
        for mount in spec["mounts"]:
            if not Path(mount["source"]).exists():
                raise JobError("Mount source is missing: " + mount["source"])
            argv.extend(["--volume", ":".join((mount["source"], mount["target"], mount["mode"]))])
        for key, value in environment(job, config, run).items():
            argv.extend(["--env", key + "=" + value])
        if spec.get("entrypoint") is not None:
            argv.extend(["--entrypoint", spec["entrypoint"]])
        argv.extend([identity, *spec["command"]])
        if not record_launch_intent(store, job):
            return
        result = call(argv, timeout=30)
        if result.returncode:
            # A definitive absence after the create call means no job is running.
            if docker_inspect(name) is None:
                raise JobError("Docker create failed: " + result.stderr.strip()[:1000])
        confirmed = docker_inspect(name)
        if confirmed is not None:
            require_docker_owner(job, confirmed)
            record_external_confirmed(store, job)
    current = store.get(job["id"])
    if current["status"] == "cancelling":
        return
    state = docker_inspect(name)
    if state is not None and state["State"]["Status"] == "created":
        result = call(["docker", "start", name], timeout=30)
        if result.returncode:
            state = docker_inspect(name)
            if state["State"]["Status"] == "created":
                # The created container holds no GPU process; remove it before releasing.
                checked(["docker", "rm", name])
                raise JobError("Docker start failed: " + result.stderr.strip()[:1000])


def start_native(store, job, config):
    name = job["runtime"]["name"]
    existing = native_inspect(name)
    if existing is not None:
        require_native_owner(job, existing)
        return
    if store.get(job["id"])["status"] == "cancelling":
        return
    spec = job["spec"]
    run = store.prepare_run(job["id"])
    if not Path(spec["cwd"]).is_dir():
        raise JobError("Working directory is missing: " + spec["cwd"])
    argv = ["systemd-run", "--user", "--quiet", "--unit", name,
            "--description", f"GPUQ {job['runtime']['instance']} job {job['id']}",
            "--working-directory", spec["cwd"],
            "--property=Type=exec", "--property=ExitType=cgroup", "--property=RemainAfterExit=yes",
            "--property=KillMode=control-group", "--property=TimeoutStopSec=20s",
            f"--property=CPUQuota={spec['cpus'] * 100}%",
            f"--property=MemoryMax={spec['memory_bytes']}", "--property=MemorySwapMax=0",
            "--property=StandardOutput=append:" + str(run / "console.log"),
            "--property=StandardError=append:" + str(run / "console.log")]
    if spec.get("timeout_seconds"):
        argv.append(f"--property=RuntimeMaxSec={spec['timeout_seconds']}s")
    for key, value in environment(job, config, run).items():
        argv.append("--setenv=" + key + "=" + value)
    argv.extend(["--", *spec["command"]])
    if not record_launch_intent(store, job):
        return
    result = call(argv, timeout=30)
    confirmed = native_inspect(name)
    if result.returncode and confirmed is None:
        raise JobError("Native job start failed: " + result.stderr.strip()[:1000])
    if confirmed is not None:
        require_native_owner(job, confirmed)
        record_external_confirmed(store, job)


def valid_docker_log_timestamp(value):
    """Validate the Docker UTC format while preserving nanosecond precision."""
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", value):
        return False
    try:
        # fromisoformat accepts "Z" and nanoseconds only from Python 3.11.
        dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return True


def saved_docker_log_cursor(path):
    """Recover a corrupt cursor from retained timestamped records, once."""
    timestamp, count = None, 0
    if not path.exists():
        return timestamp, count
    with path.open("r", newline="\n", errors="replace") as stream:
        for line in stream:
            value = line.partition(" ")[0]
            if not valid_docker_log_timestamp(value):
                continue
            if value == timestamp:
                count += 1
            else:
                timestamp, count = value, 1
    return timestamp, count


def collect_docker_logs(store, job):
    """Incremental logs with nanosecond Docker timestamps and boundary deduplication."""
    name = job["runtime"]["name"]
    run = store.prepare_run(job["id"])
    runtime = dict(job["runtime"])
    cursor = runtime.get("log_timestamp")
    boundary_count = runtime.get("log_boundary_count", 0)
    recovered = bool(cursor) and not valid_docker_log_timestamp(cursor)
    if recovered:
        cursor, boundary_count = saved_docker_log_cursor(run / "console.log")
    argv = ["docker", "logs", "--timestamps"]
    if cursor:
        argv.extend(["--since", cursor])
    argv.append(name)
    temporary = run / ".docker-logs.tmp"
    try:
        with temporary.open("w+", newline="\n") as stream:
            try:
                result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT,
                                        text=True, timeout=20, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise HostError(f"Docker log collection failed: {error}") from error
            if result.returncode:
                stream.seek(0)
                raise HostError("Docker log collection failed: " + stream.read(1000))
            stream.seek(0)
            skipped = 0
            last_timestamp = cursor
            last_count = boundary_count
            with (run / "console.log").open("a", newline="\n") as output:
                for line in stream:
                    timestamp = line.partition(" ")[0]
                    if not valid_docker_log_timestamp(timestamp):
                        output.write(line)
                        continue
                    if timestamp == cursor and skipped < boundary_count:
                        skipped += 1
                        continue
                    output.write(line)
                    if timestamp == last_timestamp:
                        last_count += 1
                    else:
                        last_timestamp, last_count = timestamp, 1
        if last_timestamp or recovered:
            runtime.update(log_timestamp=last_timestamp, log_boundary_count=last_count)
            if recovered:
                runtime["log_cursor_recovered_from_console"] = True
            store.update(job["id"], runtime=runtime)
    finally:
        temporary.unlink(missing_ok=True)


def stop_docker(job):
    name = job["runtime"]["name"]
    existing = docker_inspect(name)
    if existing is not None:
        require_docker_owner(job, existing)
    if existing is not None and existing["State"].get("Running"):
        checked(["docker", "stop", "--timeout", "20", name], timeout=25)
    elif existing is not None and existing["State"]["Status"] == "created":
        checked(["docker", "rm", name])


def stop_native(job):
    name = job["runtime"]["name"]
    existing = native_inspect(name)
    if existing is not None:
        require_native_owner(job, existing)
        systemctl("stop", name + ".service")


def cleanup(job):
    name = job["runtime"].get("name")
    if not name:
        return
    if job["spec"]["backend"] == "docker":
        existing = docker_inspect(name)
        if existing is not None:
            require_docker_owner(job, existing)
        if existing is not None and not existing["State"].get("Running"):
            checked(["docker", "rm", name])
    else:
        existing = native_inspect(name)
        if existing is not None:
            require_native_owner(job, existing)
            systemctl("stop", name + ".service")
            if native_inspect(name) is not None:
                systemctl("reset-failed", name + ".service")


def elapsed(job):
    started = dt.datetime.fromisoformat(job["started_at"])
    return (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
