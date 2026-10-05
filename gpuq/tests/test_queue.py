import concurrent.futures
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import sqlite3
from unittest.mock import patch

from gpuqueue import runtime
from gpuqueue.cli import main as cli
from gpuqueue.scheduler import choose
from gpuqueue.source import freeze
from gpuqueue.store import Store, atomic_json
from gpuqueue.worker import finish, reconcile


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.gpus = [dict(index=0, uuid="GPU-a", name="test", idle_memory_mib=400),
                     dict(index=1, uuid="GPU-b", name="test", idle_memory_mib=20)]
        self.config = dict(version=1, gpus=self.gpus, max_cpus=16,
                           max_memory_bytes=24 * 1024**3, host_memory_headroom_bytes=2 * 1024**3,
                           poll_seconds=2)
        self.observation = dict(gpus=[dict(gpu, memory_used_mib=gpu["idle_memory_mib"])
                                     for gpu in self.gpus], blocked={}, error=None,
                                memory_available_bytes=30 * 1024**3)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def enqueue(self, gpus=1, cpus=4, memory=8, dependencies=(), **values):
        spec = dict(project="test", name="job", priority=0, backend="docker", gpus=gpus,
                    cpus=cpus, memory_bytes=memory * 1024**3, gpu_uuids=[], command=["true"], env={})
        spec.update(values)
        return self.store.enqueue(spec, dependencies)

    def test_read_only_store_sees_live_wal_and_rejects_mutation(self):
        job_id = self.enqueue()
        with patch.object(Path, "mkdir", side_effect=AssertionError("cached query mkdir")), \
                patch.object(Path, "chmod", side_effect=AssertionError("cached query chmod")):
            reader = Store(self.root, read_only=True)
            try:
                self.assertEqual(reader.get(job_id)["status"], "queued")
                self.store.update(job_id, status="running")
                self.assertEqual(reader.get(job_id)["status"], "running")
                with self.assertRaises(sqlite3.OperationalError):
                    reader.set_setting("paused", True)
            finally:
                reader.close()

    def test_cached_cli_queries_do_not_initialize_state_or_outputs(self):
        atomic_json(self.root / "config.json", self.config)
        job_id = self.enqueue()
        self.store.prepare_run(job_id)
        self.store.update(job_id, status="succeeded", exit_code=0)
        self.store.set_setting("worker", {"state": "running", "heartbeat": "2026-09-28T00:00:00+00:00"})
        (self.store.run_dir(job_id) / "console.log").write_text("retained output\n")
        with patch.object(Path, "mkdir", side_effect=AssertionError("cached CLI mkdir")), \
                patch.object(Path, "chmod", side_effect=AssertionError("cached CLI chmod")), \
                patch.object(Store, "prepare_run", side_effect=AssertionError("cached CLI prepare_run")):
            for arguments in (["status", "--json"], ["show", str(job_id)],
                              ["logs", str(job_id)], ["wait", str(job_id), "--timeout", "1s"]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli(["--state-dir", str(self.root), *arguments]), 0)

    def test_two_single_gpu_jobs_do_not_overlap_reservations(self):
        first, second, third = [self.enqueue() for _ in range(3)]
        a = choose(self.store, self.config, self.observation)
        b = choose(self.store, self.config, self.observation)
        self.assertEqual([a["id"], b["id"]], [first, second])
        self.assertFalse(set(a["allocation"]) & set(b["allocation"]))
        self.assertIsNone(choose(self.store, self.config, self.observation))
        self.assertEqual(self.store.get(third)["status"], "queued")

    def test_two_gpu_job_cannot_be_starved_by_backfilling(self):
        running = self.enqueue()
        choose(self.store, self.config, self.observation)
        both = self.enqueue(gpus=2)
        smaller = self.enqueue()
        self.assertIsNone(choose(self.store, self.config, self.observation))
        self.assertEqual(self.store.get(smaller)["status"], "queued")
        self.store.update(running, status="succeeded")
        self.assertEqual(choose(self.store, self.config, self.observation)["id"], both)

    def test_dependencies_do_not_block_unrelated_ready_work(self):
        dependency = self.enqueue()
        child = self.enqueue(dependencies=[dependency], priority=50)
        unrelated = self.enqueue(priority=10)
        self.assertEqual(choose(self.store, self.config, self.observation)["id"], unrelated)
        self.assertEqual(self.store.get(child)["status"], "queued")
        self.store.update(dependency, status="failed")
        choose(self.store, self.config, self.observation)
        self.assertEqual(self.store.get(child)["status"], "skipped")

    def test_gpu_pin_and_unmanaged_work_are_respected(self):
        job_id = self.enqueue(gpu_uuids=["GPU-b"])
        self.observation["blocked"]["GPU-b"] = "unmanaged container"
        self.assertIsNone(choose(self.store, self.config, self.observation))
        self.assertIn("unmanaged", self.store.get(job_id)["detail"])
        self.observation["blocked"].clear()
        self.assertEqual(choose(self.store, self.config, self.observation)["allocation"], ["GPU-b"])

    def test_ram_and_cpu_budget_remain_reserved_until_runtime_stops(self):
        first = self.enqueue(cpus=12, memory=20)
        choose(self.store, self.config, self.observation)
        self.store.update(first, status="cancelling")
        second = self.enqueue(cpus=8, memory=8)
        self.assertIsNone(choose(self.store, self.config, self.observation))
        self.assertEqual(self.store.get(second)["status"], "queued")
        self.store.update(first, status="cancelled")
        self.assertEqual(choose(self.store, self.config, self.observation)["id"], second)

    def test_unknown_host_state_fails_closed(self):
        job_id = self.enqueue()
        self.observation["error"] = "Docker socket inaccessible"
        self.assertIsNone(choose(self.store, self.config, self.observation))
        self.assertEqual(self.store.get(job_id)["allocation"], [])
        self.observation["error"] = None
        self.observation["memory_available_bytes"] = 3 * 1024**3
        self.assertIsNone(choose(self.store, self.config, self.observation))

    def test_pause_only_blocks_new_dispatch(self):
        self.enqueue()
        self.store.set_setting("paused", True)
        self.assertIsNone(choose(self.store, self.config, self.observation))
        self.store.set_setting("paused", False)
        self.assertIsNotNone(choose(self.store, self.config, self.observation))

    def test_concurrent_projects_get_distinct_durable_ids(self):
        spec = dict(project="parallel", name="job", priority=0)
        def submit(_):
            connection = Store(self.root)
            try:
                return connection.enqueue(spec)
            finally:
                connection.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(submit, range(32)))
        self.assertEqual(len(set(ids)), 32)
        reopened = Store(self.root)
        self.assertEqual(len(reopened.jobs()), 32)
        reopened.close()

    def test_running_container_is_adopted_without_relaunch(self):
        job_id = self.enqueue()
        job = choose(self.store, self.config, self.observation)
        external = {"Config": {"Labels": {"io.gpuq.instance": job["runtime"]["instance"],
                                          "io.gpuq.job": str(job_id)}},
                    "State": {"Running": True, "Status": "running"}}
        with patch.object(runtime, "docker_inspect", return_value=external), \
                patch.object(runtime, "collect_docker_logs"), \
                patch.object(runtime, "start_docker") as start:
            reconcile(self.store, job, self.config)
            start.assert_not_called()
        self.assertEqual(self.store.get(job_id)["status"], "running")
        self.assertEqual(self.store.get(job_id)["allocation"], job["allocation"])

    def test_failed_cancellation_keeps_reservations(self):
        job_id = self.enqueue()
        job = choose(self.store, self.config, self.observation)
        self.store.update(job_id, status="cancelling")
        external = {"Config": {"Labels": {"io.gpuq.instance": job["runtime"]["instance"],
                                          "io.gpuq.job": str(job_id)}},
                    "State": {"Running": True, "Status": "running"}}
        with patch.object(runtime, "docker_inspect", return_value=external), \
                patch.object(runtime, "stop_docker", side_effect=runtime.HostError("Docker offline")):
            with self.assertRaises(runtime.HostError):
                reconcile(self.store, self.store.get(job_id), self.config)
        self.assertEqual(self.store.get(job_id)["status"], "cancelling")
        self.assertEqual(self.store.get(job_id)["allocation"], job["allocation"])

    def test_completion_observes_a_cancellation_and_redacts_env(self):
        job_id = self.enqueue(env={"PRIVATE_TOKEN": "do-not-copy"})
        job = choose(self.store, self.config, self.observation)
        self.store.update(job_id, status="cancelling")
        finish(self.store, job, "succeeded", 0, "Completed")
        self.assertEqual(self.store.get(job_id)["status"], "cancelled")
        self.assertNotIn("do-not-copy", (self.store.run_dir(job_id) / "manifest.json").read_text())

    def test_unowned_containers_and_units_are_not_managed(self):
        job = choose(self.store, self.config, self.observation) if self.enqueue() else None
        with self.assertRaises(runtime.HostError):
            runtime.require_docker_owner(job, {"Config": {"Labels": {}}})
        with self.assertRaises(runtime.HostError):
            runtime.require_native_owner(job, {"Description": "unrelated"})

    def test_a_second_worker_cannot_enter(self):
        atomic_json(self.root / "config.json", self.config)
        with (self.root / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            executable = Path(__file__).resolve().parents[1] / "gpuq"
            result = subprocess.run(["python3", str(executable), "--state-dir", str(self.root),
                                     "worker", "--once"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("already holds", result.stderr)

    def test_source_snapshot_survives_later_working_tree_edits(self):
        project = self.root / "project"
        project.mkdir()
        subprocess.run(["git", "init", "--quiet", str(project)], check=True)
        (project / "train.py").write_text("print('version 1')\n")
        snapshot = freeze(project, self.root)
        (project / "train.py").write_text("print('version 2')\n")
        self.assertEqual((snapshot / "train.py").read_text(), "print('version 1')\n")
        files = json.loads((snapshot / ".gpuq-source.json").read_text())["files"]
        self.assertEqual(files[0]["path"], "train.py")
        self.assertEqual(len(files[0]["sha256"]), 64)

    def test_confirmed_runtime_missing_before_first_poll_is_not_replayed(self):
        job_id = self.enqueue()
        job = choose(self.store, self.config, self.observation)
        self.store.update(job_id, runtime=dict(job["runtime"], external_confirmed=True))
        with patch.object(runtime, "docker_inspect", return_value=None), \
                patch.object(runtime, "start_docker") as start:
            reconcile(self.store, self.store.get(job_id), self.config)
            start.assert_not_called()
        self.assertEqual(self.store.get(job_id)["status"], "failed")

    def test_uncertain_launch_from_previous_boot_is_not_replayed(self):
        job_id = self.enqueue()
        job = choose(self.store, self.config, self.observation)
        self.store.update(job_id, runtime=dict(job["runtime"], launch_attempted=True,
                                               launch_boot_id="previous-boot"))
        with patch.object(runtime, "docker_inspect", return_value=None), \
                patch.object(runtime, "boot_id", return_value="current-boot"), \
                patch.object(runtime, "start_docker") as start:
            reconcile(self.store, self.store.get(job_id), self.config)
            start.assert_not_called()
        self.assertEqual(self.store.get(job_id)["status"], "failed")

    def test_gpu_metadata_records_observed_indices_after_reordering(self):
        self.observation["gpus"][0]["index"] = 1
        self.observation["gpus"][1]["index"] = 0
        self.enqueue(gpu_uuids=["GPU-b"])
        job = choose(self.store, self.config, self.observation)
        job["spec"]["backend"] = "native"
        values = runtime.environment(job, self.config, self.root)
        self.assertEqual(values["GPUQ_GPU_INDICES"], "0")
        self.assertEqual(values["CUDA_VISIBLE_DEVICES"], "GPU-b")

    def test_explicit_gpu_order_is_preserved(self):
        self.enqueue(gpus=2, gpu_uuids=["GPU-a", "GPU-b"])
        job = choose(self.store, self.config, self.observation)
        self.assertEqual(job["allocation"], ["GPU-a", "GPU-b"])

    def test_cli_resolves_gpu_indices_from_latest_host_observation(self):
        atomic_json(self.root / "config.json", self.config)
        current = [dict(self.gpus[0], index=1), dict(self.gpus[1], index=0)]
        self.store.set_setting("worker", {"snapshot": {"gpus": current}})
        with contextlib.redirect_stdout(io.StringIO()):
            result = cli(["--state-dir", str(self.root), "submit", "--cwd", str(self.root),
                          "--project", "mapped", "--image", "example:local", "--gpu", "0",
                          "--", "true"])
        self.assertEqual(result, 0)
        self.assertEqual(self.store.jobs()[0]["spec"]["gpu_uuids"], ["GPU-b"])

    def test_submission_records_labels_and_agent_origin(self):
        atomic_json(self.root / "config.json", self.config)
        origin = {"CODEX_TASK_RUN": "20261003-run", "CODEX_TASK_TURN": "2"}
        with patch.dict(os.environ, origin), contextlib.redirect_stdout(io.StringIO()):
            result = cli(["--state-dir", str(self.root), "submit", "--cwd", str(self.root),
                          "--image", "example:local", "--label", "seed=3",
                          "--label", "experiment=ablation", "--", "true"])
        self.assertEqual(result, 0)
        spec = self.store.jobs()[0]["spec"]
        self.assertEqual(spec["labels"], {"seed": "3", "experiment": "ablation"})
        self.assertEqual(spec["origin"]["codex_run"], "20261003-run")
        self.assertEqual(spec["origin"]["codex_turn"], "2")

    def test_retry_records_the_retrying_origin_not_the_original(self):
        atomic_json(self.root / "config.json", self.config)
        job_id = self.enqueue(origin={"codex_run": "run-a", "codex_turn": "1"}, labels={"seed": "3"})
        self.store.update(job_id, status="failed", finished_at="2026-10-03T00:00:00+00:00")
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("CODEX_TASK_", "CLAUDE_CODE_SESSION"))}
        environment["CODEX_TASK_RUN"] = "run-b"
        with patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["--state-dir", str(self.root), "retry", str(job_id)]), 0)
        retry = self.store.jobs()[-1]["spec"]
        self.assertEqual(retry["origin"], {"codex_run": "run-b"})
        self.assertEqual(retry["labels"], {"seed": "3"})
        self.assertEqual(retry["retry_of"], job_id)

    def test_invalid_label_is_rejected_before_enqueue(self):
        atomic_json(self.root / "config.json", self.config)
        with contextlib.redirect_stderr(io.StringIO()):
            result = cli(["--state-dir", str(self.root), "submit", "--cwd", str(self.root),
                          "--image", "example:local", "--label", "no-separator", "--", "true"])
        self.assertEqual(result, 2)
        self.assertEqual(self.store.jobs(), [])

    def test_absolute_symlink_is_frozen_inside_snapshot(self):
        project = self.root / "project"
        project.mkdir()
        subprocess.run(["git", "init", "--quiet", str(project)], check=True)
        (project / "original.py").write_text("version 1")
        (project / "linked.py").symlink_to(project / "original.py")
        snapshot = freeze(project, self.root)
        (project / "original.py").write_text("version 2")
        self.assertTrue((snapshot / "linked.py").resolve().is_relative_to(snapshot))
        self.assertEqual((snapshot / "linked.py").read_text(), "version 1")

    def test_environment_file_cannot_override_gpu_reservation(self):
        env_file = self.root / "job.env"
        env_file.write_text("CUDA_VISIBLE_DEVICES=all\n")
        job_id = self.enqueue(env_files=[str(env_file)])
        job = choose(self.store, self.config, self.observation)
        with self.assertRaises(runtime.JobError):
            runtime.environment(job, self.config, self.root)

    def test_project_status_keeps_other_projects_gpu_reservations_visible(self):
        atomic_json(self.root / "config.json", self.config)
        self.enqueue()
        job = choose(self.store, self.config, self.observation)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli(["--state-dir", str(self.root), "status", "--project", "other-project"])
        self.assertEqual(result, 0)
        self.assertIn(f"GPU 1: test | job {job['id']}", output.getvalue())

    def native_venv(self):
        """A real symlinked interpreter and import available only in this venv."""
        import venv
        project = self.root / 'native-project'
        project.mkdir()
        environment = project / '.venv'
        venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
        python = environment / 'bin/python'
        self.assertTrue(python.is_symlink())
        result = subprocess.run([str(python), '-c',
                                 'import sysconfig; print(sysconfig.get_path("purelib"))'],
                                check=True, capture_output=True, text=True)
        packages = Path(result.stdout.strip())
        (packages / 'gpuq_venv_marker.py').write_text('VALUE = "venv-only-import"\n')
        atomic_json(self.root / 'config.json', self.config)
        return project, environment, python

    def submit_native_probe(self, project, executable, snapshot=False):
        arguments = ['--state-dir', str(self.root), 'submit', '--cwd', str(project),
                     '--project', 'native-probe', '--gpus', '0', '--cpus', '1', '--memory', '128m']
        if snapshot:
            arguments.append('--snapshot')
        code = ('import json, sys, gpuq_venv_marker; '
                'print(json.dumps({"prefix": sys.prefix, "marker": gpuq_venv_marker.VALUE}))')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli([*arguments, '--', str(executable), '-c', code]), 0)
        return self.store.jobs()[-1]['spec']

    def assert_native_venv_probe(self, spec, environment, python):
        self.assertEqual(spec['command'][0], str(python))
        result = subprocess.run(spec['command'], cwd=spec['cwd'], check=True,
                                capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout),
                         {'prefix': str(environment), 'marker': 'venv-only-import'})

    def test_absolute_native_python_preserves_virtual_environment(self):
        project, environment, python = self.native_venv()
        self.assert_native_venv_probe(self.submit_native_probe(project, python), environment, python)

    def test_relative_native_python_preserves_virtual_environment(self):
        project, environment, python = self.native_venv()
        self.assert_native_venv_probe(self.submit_native_probe(project, '.venv/bin/python'), environment, python)

    def test_path_native_python_preserves_virtual_environment(self):
        project, environment, python = self.native_venv()
        with patch.dict(os.environ, {'PATH': str(python.parent) + os.pathsep + os.environ.get('PATH', '')}):
            self.assert_native_venv_probe(self.submit_native_probe(project, 'python'), environment, python)

    def test_relative_path_entry_native_python_becomes_absolute(self):
        project, environment, python = self.native_venv()
        previous = Path.cwd()
        try:
            os.chdir(project)
            with patch.dict(os.environ, {'PATH': '.venv/bin' + os.pathsep + os.environ.get('PATH', '')}):
                spec = self.submit_native_probe(project, 'python')
            self.assert_native_venv_probe(spec, environment, python)
        finally:
            os.chdir(previous)

    def test_snapshot_keeps_ignored_virtual_environment_external(self):
        project, environment, python = self.native_venv()
        subprocess.run(['git', 'init', '--quiet', str(project)], check=True)
        (project / '.gitignore').write_text('.venv/\n')
        (project / 'train.py').write_text('print("frozen source")\n')
        spec = self.submit_native_probe(project, python, snapshot=True)
        self.assertNotEqual(spec['cwd'], str(project))
        self.assertFalse((Path(spec['cwd']) / '.venv').exists())
        self.assert_native_venv_probe(spec, environment, python)

    def test_snapshot_remaps_included_native_executable(self):
        project = self.root / 'native-wrapper-project'
        project.mkdir()
        subprocess.run(['git', 'init', '--quiet', str(project)], check=True)
        wrapper = project / 'run.sh'
        wrapper.write_text('#!/bin/sh\nprintf "frozen-wrapper"\n')
        wrapper.chmod(0o755)
        atomic_json(self.root / 'config.json', self.config)
        with contextlib.redirect_stdout(io.StringIO()):
            result = cli(['--state-dir', str(self.root), 'submit', '--cwd', str(project),
                          '--project', 'native-wrapper', '--gpus', '0', '--snapshot', '--', str(wrapper)])
        self.assertEqual(result, 0)
        spec = self.store.jobs()[-1]['spec']
        self.assertEqual(spec['command'][0], str(Path(spec['cwd']) / 'run.sh'))
        wrapper.write_text('#!/bin/sh\nprintf "changed-wrapper"\n')
        output = subprocess.run(spec['command'], cwd=spec['cwd'], check=True,
                                capture_output=True, text=True)
        self.assertEqual(output.stdout, 'frozen-wrapper')


if __name__ == "__main__":
    unittest.main()
