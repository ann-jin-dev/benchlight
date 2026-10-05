"""Regression tests for CR progress output and corrupt persisted cursors."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gpuqueue import runtime
from gpuqueue.store import Store

T1 = "2026-09-29T13:34:37.547387137Z"
T2 = "2026-09-29T13:35:03.716103403Z"


class DockerLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.job_id = self.store.enqueue({"project": "test", "name": "logs", "backend": "docker", "priority": 0,
            "gpus": 0, "cpus": 1, "memory_bytes": 1024, "command": ["true"], "env": {}})
        self.store.update(self.job_id, status="running", runtime={"name": "owned-test-container"})
        self.log = self.store.prepare_run(self.job_id) / "console.log"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def collect(self, payload):
        def response(argv, **kwargs):
            kwargs["stdout"].buffer.write(payload.encode())
            kwargs["stdout"].flush()
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(runtime.subprocess, "run", side_effect=response) as run:
            runtime.collect_docker_logs(self.store, self.store.get(self.job_id))
        return run.call_args.args[0]

    def test_progress_carriage_returns_preserved_and_cursor_is_timestamp(self):
        payload = T1 + " \rFetching 12 files: 8/12\rFetching 12 files: 10/12\n"
        self.collect(payload)
        self.assertEqual(self.log.read_bytes(), payload.encode())
        job = self.store.get(self.job_id)
        self.assertEqual(job["runtime"]["log_timestamp"], T1)
        self.assertEqual(job["runtime"]["log_boundary_count"], 1)
        argv = self.collect(payload + T2 + " finished\n")
        self.assertEqual(argv[argv.index("--since") + 1], T1)
        self.assertEqual(self.log.read_bytes(), (payload + T2 + " finished\n").encode())

    def test_corrupt_fetching_cursor_recovered_without_replaying_saved_boundary(self):
        retained = T1 + " \nFetching 12 files: 10/12\n"
        self.log.write_text(retained)
        self.store.update(self.job_id, runtime={"name": "owned-test-container", "log_timestamp": "Fetching", "log_boundary_count": 1})
        argv = self.collect(T1 + " \rFetching 12 files: 10/12\n" + T2 + " done\n")
        self.assertEqual(argv[argv.index("--since") + 1], T1)
        self.assertEqual(self.log.read_text(), retained + T2 + " done\n")
        job = self.store.get(self.job_id)
        self.assertEqual(job["runtime"]["log_timestamp"], T2)
        self.assertTrue(job["runtime"]["log_cursor_recovered_from_console"])
        self.assertEqual(job["status"], "running")

    def test_same_nanosecond_boundary_count_is_preserved(self):
        self.collect(T1 + " a\n" + T1 + " b\n")
        self.collect(T1 + " a\n" + T1 + " b\n" + T1 + " c\n")
        self.assertEqual(self.log.read_text(), T1 + " a\n" + T1 + " b\n" + T1 + " c\n")
        self.assertEqual(self.store.get(self.job_id)["runtime"]["log_boundary_count"], 3)

    def test_unframed_output_cannot_poison_cursor(self):
        payload = T1 + " good\nFetching unframed progress\n"
        self.collect(payload)
        self.assertEqual(self.log.read_text(), payload)
        self.assertEqual(self.store.get(self.job_id)["runtime"]["log_timestamp"], T1)

    def test_invalid_cursor_with_no_retained_timestamps_is_reset(self):
        self.store.update(self.job_id, runtime={"name": "owned-test-container", "log_timestamp": "Fetching", "log_boundary_count": 1})
        argv = self.collect("")
        self.assertNotIn("--since", argv)
        self.assertIsNone(self.store.get(self.job_id)["runtime"]["log_timestamp"])
        self.assertEqual(self.store.get(self.job_id)["runtime"]["log_boundary_count"], 0)

    def test_timestamp_validation_keeps_nanoseconds_and_rejects_invalid_calendar(self):
        self.assertTrue(runtime.valid_docker_log_timestamp(T1))
        for value in ["Fetching", "2026-99-29T13:34:37Z", "2026-09-29T13:34:37.1234567890Z", None]:
            self.assertFalse(runtime.valid_docker_log_timestamp(value))


if __name__ == "__main__":
    unittest.main()
