#!/usr/bin/env python3
"""Real workstation verification, using only small jobs owned by this test.

Run after install.py, with an idle queue and a locally verified Torch image.
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gpuqueue.store import ACTIVE, TERMINAL, Store, atomic_json, default_root, load_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    root = default_root()
    config = load_config(root)
    store = Store(root)
    project_path = Path(__file__).resolve().parents[1]
    executable = str(project_path / "gpuq")
    project = "queue-verification-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jobs = []
    foreign = None
    report = {"project": project, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "checks": [], "jobs": jobs, "gpus": config["gpus"]}

    def command(*argv, check=True):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=40, check=False)
        if check and result.returncode:
            raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {result.stderr or result.stdout}")
        return result.stdout.strip()

    def cli(*argv):
        return command(executable, *map(str, argv))

    def until(predicate, timeout=70):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.25)
        states = [(job_id, store.get(job_id)["status"], store.get(job_id)["detail"]) for job_id in jobs]
        raise AssertionError("Timed out; jobs: " + str(states))

    def completed(job_ids):
        until(lambda: all(store.get(job_id)["status"] in TERMINAL for job_id in job_ids))

    def gpu(devices=1, hold=8, *options):
        job_id = int(cli("submit", "--project", project, "--name", f"cuda-{devices}-gpu",
                         "--cwd", str(project_path / "examples"), "--image", args.image,
                         "--gpus", devices, "--cpus", 2, "--memory", "2g", "--shm-size", "512m",
                         *options, "--", "python", "gpu_smoke.py", "--devices", devices, "--hold", hold))
        jobs.append(job_id)
        return job_id

    def native(*argv, options=()):
        job_id = int(cli("submit", "--project", project, "--name", "native-check",
                         "--cwd", str(project_path), "--gpus", 0, "--cpus", 1, "--memory", "256m",
                         *options, "--", *argv))
        jobs.append(job_id)
        return job_id

    def read_output(job_id):
        return json.loads((store.run_dir(job_id) / "outputs/smoke.json").read_text())

    def inspect_job(job_id):
        job = store.get(job_id)
        if job["spec"]["backend"] == "docker":
            container = json.loads(command("docker", "inspect", job["runtime"]["name"]))[0]
            return {"id": container["Id"], "pid": container["State"]["Pid"],
                    "memory": container["HostConfig"]["Memory"],
                    "cpus": container["HostConfig"]["NanoCpus"],
                    "requests": container["HostConfig"]["DeviceRequests"]}
        return command("systemctl", "--user", "show", job["runtime"]["name"] + ".service",
                       "--property=MainPID,MemoryMax,CPUQuotaPerSecUSec")

    def passed(name, **data):
        report["checks"].append(dict(name=name, passed=True, **data))
        print("PASS: " + name, flush=True)

    if store.jobs(("queued", *ACTIVE)) or store.setting("paused", False):
        raise SystemExit("Host verification needs an idle, unpaused queue")
    until(lambda: store.setting("worker", {}).get("state") == "running", timeout=20)
    success = False
    try:
        cli("pause")
        first, second, both = gpu(), gpu(), gpu(2, 2)
        cli("resume")
        until(lambda: all((store.run_dir(job_id) / "outputs/smoke.json").exists()
                          for job_id in (first, second)))
        assert store.get(both)["status"] == "queued"
        first_runtime, second_runtime = inspect_job(first), inspect_job(second)
        assert first_runtime["memory"] == second_runtime["memory"] == 2 * 1024**3
        assert first_runtime["cpus"] == second_runtime["cpus"] == 2 * 10**9
        completed([first, second, both])
        assert all(store.get(job_id)["status"] == "succeeded" for job_id in (first, second, both))
        a, b, c = [read_output(job_id) for job_id in (first, second, both)]
        assert set(a["uuids"]).isdisjoint(b["uuids"])
        assert max(a["compute_start"], b["compute_start"]) < min(a["compute_end"], b["compute_end"])
        assert c["compute_start"] >= max(a["compute_end"], b["compute_end"])
        assert set(c["uuids"]) == set(a["uuids"] + b["uuids"])
        report["runtime"] = {"torch": a["torch"], "cuda": a["cuda"],
                             "image_id": store.get(first)["runtime"]["image_id"]}
        passed("parallel isolated single-GPU jobs, followed by exclusive two-GPU job",
               single_jobs=[first, second], both_job=both)
        passed("Docker CPU and RAM limits", cpus=2, memory_bytes=2 * 1024**3)

        failing = native("/usr/bin/python3", "-c", "raise SystemExit(7)")
        dependent = native("/usr/bin/python3", "-c", "raise AssertionError('dependency ran')",
                           options=("--after", failing))
        completed([failing, dependent])
        assert store.get(failing)["status"] == "failed" and store.get(failing)["exit_code"] == 7
        assert store.get(dependent)["status"] == "skipped"
        retry = int(cli("retry", failing))
        jobs.append(retry)
        completed([retry])
        assert retry != failing and store.get(retry)["spec"]["retry_of"] == failing
        assert store.get(retry)["exit_code"] == 7
        passed("failure, dependency skip, and a separate retry attempt")

        native_timeout = native("/bin/sleep", "60", options=("--timeout", "2s"))
        completed([native_timeout])
        assert store.get(native_timeout)["status"] == "failed"
        assert store.get(native_timeout)["exit_code"] == 124
        docker_timeout = int(cli("submit", "--project", project, "--name", "docker-timeout",
                                 "--cwd", project_path, "--image", "nvidia/cuda:12.8.1-base-ubuntu24.04",
                                 "--gpus", 1, "--cpus", 1, "--memory", "256m", "--shm-size", "16m",
                                 "--timeout", "2s", "--", "sleep", "60"))
        jobs.append(docker_timeout)
        completed([docker_timeout])
        assert store.get(docker_timeout)["status"] == "failed"
        assert store.get(docker_timeout)["exit_code"] == 124
        passed("native and Docker time limits release resources")

        children = native("/bin/bash", "-c",
                          'sleep 120 & queue_child_pid=$!; echo "$queue_child_pid" > "$GPUQ_OUTPUT_DIR/child.pid"; wait')
        child_file = store.run_dir(children) / "outputs/child.pid"
        until(child_file.exists)
        child_pid = int(child_file.read_text())
        native_limit = inspect_job(children)
        assert "MemoryMax=268435456" in native_limit and "CPUQuotaPerSecUSec=1s" in native_limit
        cli("cancel", children)
        completed([children])
        assert store.get(children)["status"] == "cancelled"
        child_stat = Path(f"/proc/{child_pid}/stat")
        assert not child_stat.exists() or child_stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
        passed("native CPU/RAM limits and cancellation of the whole child process group")

        recovering_docker = gpu(1, 120)
        recovering_native = native("/usr/bin/python3", "-c", "import time; print('started', flush=True); time.sleep(120)")
        until(lambda: (store.run_dir(recovering_docker) / "outputs/smoke.json").exists() and
                      store.get(recovering_native)["status"] == "running")
        docker_before, native_before = inspect_job(recovering_docker), inspect_job(recovering_native)
        old_worker = store.setting("worker")["pid"]
        command("systemctl", "--user", "restart", "gpuq.service")
        until(lambda: store.setting("worker").get("pid") != old_worker)
        assert inspect_job(recovering_docker)["pid"] == docker_before["pid"]
        assert inspect_job(recovering_native) == native_before
        old_worker = store.setting("worker")["pid"]
        command("systemctl", "--user", "kill", "--kill-whom=main", "--signal=SIGKILL", "gpuq.service")
        until(lambda: store.setting("worker").get("pid") != old_worker)
        assert inspect_job(recovering_docker)["id"] == docker_before["id"]
        assert inspect_job(recovering_docker)["pid"] == docker_before["pid"]
        assert inspect_job(recovering_native) == native_before
        cli("cancel", recovering_docker, recovering_native)
        completed([recovering_docker, recovering_native])
        assert all(store.get(job_id)["status"] == "cancelled" for job_id in (recovering_docker, recovering_native))
        log = (store.run_dir(recovering_docker) / "console.log").read_text()
        assert log.count('"compute_start"') == 1
        passed("worker restart and SIGKILL recovery preserve Docker and native PIDs, reservations, and logs")

        device = config["gpus"][-1]
        foreign = "gpuq-verification-foreign-" + uuid.uuid4().hex[:12]
        command("docker", "run", "-d", "--name", foreign,
                "--label", "io.gpuq.verification=" + project,
                "--gpus", '"device=' + device["uuid"] + '"',
                "nvidia/cuda:12.8.1-base-ubuntu24.04", "sleep", "90")
        until(lambda: "unmanaged container" in store.setting("worker").get("snapshot", {}).get(
            "blocked", {}).get(device["uuid"], ""))
        guarded = gpu(1, 1, "--gpu", str(device["index"]))
        until(lambda: "unmanaged container" in store.get(guarded)["detail"])
        assert store.get(guarded)["status"] == "queued" and not store.get(guarded)["allocation"]
        command("docker", "stop", "--timeout", "1", foreign)
        command("docker", "rm", foreign)
        foreign = None
        completed([guarded])
        assert store.get(guarded)["status"] == "succeeded"
        passed("sleeping unmanaged GPU containers block queued jobs until they release the device")
        assert command("systemctl", "--user", "is-enabled", "gpuq.service") == "enabled"
        assert command("loginctl", "show-user", str(os.getuid()), "-p", "Linger") == "Linger=yes"
        passed("service enabled with user lingering for logout/reboot persistence")
        success = True
    finally:
        cli("resume")
        if foreign:
            command("docker", "stop", "--timeout", "1", foreign, check=False)
            command("docker", "rm", foreign, check=False)
        unfinished = [job_id for job_id in jobs if store.get(job_id)["status"] not in TERMINAL]
        if unfinished:
            cli("cancel", *unfinished)
            completed(unfinished)
        report.update(passed=success, finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        directory = project_path / "verification"
        atomic_json(directory / (project + ".json"), report)
        print("Verification record: " + str(directory / (project + ".json")), flush=True)
        store.close()


if __name__ == "__main__":
    main()
