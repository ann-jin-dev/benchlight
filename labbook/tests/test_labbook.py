import copy
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "labbook"))
sys.path.insert(0, str(ROOT / "gpuq"))

from gpuqueue.store import Store  # noqa: E402
from lab.config import DEFAULTS  # noqa: E402
from lab.core import Lab  # noqa: E402
from lab.public import public_snapshot  # noqa: E402
from lab.receipts import canonical_digest, verify  # noqa: E402

SECRET_NAME = "secret-ablation-name"
SECRET_PATH = "/home/someone/private-project"


class Fixture:
    """A small lab: three gpuq jobs, one Codex run, an energy record."""

    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.gpuq_dir, self.pulse_dir = root / "gpuq", root / "pulse"
        self.runs_dir, self.sessions_dir = root / "runs", root / "sessions"
        self.pulse_dir.mkdir()
        store = Store(self.gpuq_dir)
        self.instance = store.setting("instance")
        run = "20261003-120000-demo"
        start = time.time() - 3600

        def spec(name, cwd, origin=None, gpus=1):
            return {"project": "demo-project", "name": name, "priority": 0, "backend": "docker",
                    "image": "demo:1", "command": ["python", "train.py", "--secret-flag"], "cwd": cwd,
                    "original_cwd": cwd, "gpus": gpus, "cpus": 4, "memory_bytes": 8 * 1024**3,
                    "gpu_uuids": [], "env": {"TOKEN": "x"}, "mounts": [], "origin": origin or {},
                    "git_at_submission": {"commit": "a" * 40, "dirty": False}}

        def iso(t):
            import datetime as dt
            return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()

        self.exact_env = store.enqueue(spec(SECRET_NAME, SECRET_PATH, {"codex_run": run, "codex_turn": "1"}))
        self.exact_output = store.enqueue(spec("printed", "/elsewhere"))
        self.inferred = store.enqueue(spec("inferred", SECRET_PATH + "/sub"))
        self.unlinked = store.enqueue(spec("manual", "/tmp/manual"))
        self.claude = store.enqueue(spec("claude-direct", SECRET_PATH, {"claude_session": "abcdef0123456789"}))
        for job_id in (self.exact_env, self.exact_output, self.inferred, self.unlinked, self.claude):
            store.update(job_id, status="succeeded", exit_code=0, started_at=iso(start + 600),
                         finished_at=iso(start + 1800), runtime={"instance": self.instance, "image_id": "sha256:" + "b" * 64,
                                                                  "gpus": [{"index": 0, "uuid": "GPU-abcdef123456", "name": "Test GPU"}]})
            store.db.execute("UPDATE jobs SET created_at=? WHERE id=?", (iso(start + 300), job_id))
        store.close()

        turn = self.runs_dir / run / "turns" / "1"
        turn.mkdir(parents=True)
        (self.runs_dir / run / "meta.json").write_text(json.dumps({
            "id": run, "name": "demo", "cwd": SECRET_PATH, "model": "test-model", "effort": "high",
            "thread_id": "thread-1", "turns": 1, "wakes": 0, "max_wakes": 20}))
        (turn / "prompt.md").write_text("Do the thing.\n\n---\n## Operating protocol (from the supervising agent)\nrules")
        (turn / "started").write_text(iso(start).replace("+00:00", "+00:00"))
        (turn / "ended").write_text(json.dumps({"source": "runner", "at": iso(start + 2400)}))
        (turn / "final.md").write_text("## Summary\nIt worked.\n## Results\nnumbers\nSTATUS: DONE")
        (turn / "turn_id").write_text("turn-1")
        (turn / "events.jsonl").write_text(json.dumps({"type": "item", "item": {
            "type": "commandExecution", "command": "/bin/bash -lc 'gpuq submit --image demo:1 -- python x.py'",
            "aggregatedOutput": f"{self.exact_output}\n", "exitCode": 0}}) + "\n")
        (self.runs_dir / run / "notes.jsonl").write_text(json.dumps(
            {"at": iso(start + 2500), "by": "claude", "kind": "verdict", "turn": 1, "text": "Checked: holds"}) + "\n")
        session = self.sessions_dir / "2026" / "10" / "03"
        session.mkdir(parents=True)
        (session / "rollout-2026-10-03T12-00-00-thread-1.jsonl").write_text(json.dumps({
            "type": "token_usage_record", "payload": {"turn_id": "turn-1", "turn_token_usage": {
                "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10,
                "reasoning_output_tokens": 5, "total_tokens": 110}}}) + "\n")
        self.run = run

        db = sqlite3.connect(self.pulse_dir / "energy.sqlite3")
        db.executescript("""
            CREATE TABLE jobs (queue_instance TEXT, job_id INTEGER, project TEXT, gpu_uuids TEXT,
                gpu_mj REAL, cpu_mj REAL, cpu_seconds REAL, gpu_metered_seconds REAL,
                cpu_metered_seconds REAL, first_at REAL, last_at REAL, PRIMARY KEY (queue_instance, job_id));
            CREATE TABLE job_minutes (queue_instance TEXT, job_id INTEGER, minute INTEGER, gpu_mj REAL,
                cpu_mj REAL, seconds REAL, PRIMARY KEY (queue_instance, job_id, minute));
            CREATE TABLE days (day TEXT PRIMARY KEY, gpu_mj REAL, cpu_mj REAL, job_gpu_mj REAL,
                job_cpu_mj REAL, gpu_seconds REAL, cpu_seconds REAL);
        """)
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (self.instance, self.exact_env, "demo-project", "GPU-abcdef123456",
                    3.6e9, 0.36e9, 600, 1200, 600, start + 600, start + 1800))
        db.execute("INSERT INTO job_minutes VALUES (?,?,?,?,?,?)", (self.instance, self.exact_env, 60, 12e6, 1e6, 60))
        db.commit()
        db.close()

    def config(self, **electricity):
        config = copy.deepcopy(DEFAULTS)
        config.update(gpuq_state_dir=str(self.gpuq_dir), pulse_state_dir=str(self.pulse_dir),
                      codex_runs_dir=str(self.runs_dir), codex_sessions_dir=str(self.sessions_dir))
        config["electricity"].update(electricity)
        return config


class LabTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.lab = Lab(self.fixture.config())

    def tearDown(self):
        self.fixture.temporary.cleanup()

    def test_agent_links_are_exact_inferred_or_absent(self):
        f = self.fixture
        links = {job_id: self.lab.link(self.lab.gpuq.job(job_id))
                 for job_id in (f.exact_env, f.exact_output, f.inferred, f.unlinked)}
        self.assertEqual(links[f.exact_env]["link"], "exact")
        self.assertIn("CODEX_TASK_RUN", links[f.exact_env]["evidence"])
        self.assertEqual(links[f.exact_output]["link"], "exact")
        self.assertIn("printed", links[f.exact_output]["evidence"])
        self.assertEqual(links[f.inferred]["link"], "inferred")
        self.assertIsNone(links[f.unlinked])

    def test_receipt_joins_energy_tokens_and_verdicts(self):
        receipt = self.lab.receipt(self.fixture.exact_env)
        energy = receipt["energy"]
        self.assertEqual(energy["gpu_wh"], 1000.0)
        self.assertEqual(energy["cpu_wh"], 100.0)
        self.assertEqual(energy["gpu_coverage"], 1.0)
        self.assertIsNone(receipt["pricing"])  # never guessed
        self.assertEqual(receipt["agent"]["turn_tokens"]["total_tokens"], 110)
        self.assertEqual(receipt["agent"]["notes"][0]["kind"], "verdict")
        self.assertEqual(receipt["code"]["git_commit"], "a" * 40)

    def test_cost_and_co2_appear_only_when_configured(self):
        lab = Lab(self.fixture.config(price_per_kwh=0.30, grid_gco2_per_kwh=200))
        receipt = lab.receipt(self.fixture.exact_env)
        self.assertAlmostEqual(receipt["pricing"]["cost"]["amount"], 0.33)
        self.assertAlmostEqual(receipt["pricing"]["co2_grams"], 220.0)
        self.assertEqual(receipt["digest"], self.lab.receipt(self.fixture.exact_env)["digest"])  # tariff is outside the digest

    def test_digest_covers_what_cannot_change_after_the_job(self):
        receipt = self.lab.receipt(self.fixture.exact_env)
        self.assertTrue(verify(receipt))
        tampered = json.loads(json.dumps(receipt))
        tampered["energy"]["gpu_wh"] += 1
        self.assertFalse(verify(tampered))
        later = json.loads(json.dumps(receipt))
        later["agent"]["notes"].append({"by": "human", "kind": "decision", "text": "added later"})
        later["artifacts"]["output_files"] += 3
        self.assertTrue(verify(later))

    def test_a_copy_that_went_through_javascript_still_verifies(self):
        def javascript(value):  # JSON.stringify writes 1.0 as 1
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, dict):
                return {k: javascript(v) for k, v in value.items()}
            if isinstance(value, list):
                return [javascript(v) for v in value]
            return value
        receipt = self.lab.receipt(self.fixture.exact_env)
        self.assertEqual(receipt["energy"]["gpu_coverage"], 1.0)
        self.assertTrue(verify(json.loads(json.dumps(javascript(receipt)))))

    def test_claude_session_origin_beats_an_inferred_codex_turn(self):
        link = self.lab.link(self.lab.gpuq.job(self.fixture.claude))
        self.assertEqual(link["link"], "exact")
        self.assertIsNone(link["run"])
        self.assertEqual(link["claude_session"], "abcdef01")

    def test_printed_job_id_counts_only_if_created_during_the_turn(self):
        store = Store(self.fixture.gpuq_dir)
        store.db.execute("UPDATE jobs SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (self.fixture.exact_output,))
        store.close()
        self.assertIsNone(self.lab.link(self.lab.gpuq.job(self.fixture.exact_output)))

    def test_a_turn_without_an_end_marker_closes_at_its_last_event(self):
        turn = self.fixture.runs_dir / self.fixture.run / "turns" / "1"
        (turn / "ended").unlink()
        import os
        last = time.time() - 3500  # events.jsonl last written before the job was created
        os.utime(turn / "events.jsonl", (last, last))
        self.lab._windows = (0.0, [])
        self.assertIsNone(self.lab.link(self.lab.gpuq.job(self.fixture.inferred)))

    def test_timeline_orders_events_and_strips_the_protocol(self):
        timeline = self.lab.timeline(self.fixture.run)
        kinds = [event["kind"] for event in timeline["events"]]
        self.assertEqual(kinds[0], "brief")
        self.assertIn("report", kinds)
        self.assertIn("verdict", kinds)
        self.assertEqual(timeline["events"], sorted(timeline["events"], key=lambda e: e["at"]))
        self.assertNotIn("Operating protocol", timeline["events"][0]["text"])
        self.assertEqual(timeline["totals"]["verdicts"], 1)
        self.assertEqual(timeline["totals"]["tokens"], 110)
        self.assertEqual(timeline["totals"]["jobs"], {"succeeded": 3})  # the Claude-session job is not this run's

    def test_public_window_is_an_allowlist(self):
        text = json.dumps(public_snapshot(self.lab))
        for secret in (SECRET_NAME, SECRET_PATH, "--secret-flag", "demo-project", "TOKEN", "train.py", "thread-1"):
            self.assertNotIn(secret, text)
        self.lab.config["public"]["project_aliases"] = {"demo-project": "Vision study"}
        self.assertIn("Vision study", json.dumps(public_snapshot(self.lab)))


class ServerTests(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from lab import server
        self.fixture = Fixture()
        lab = Lab(self.fixture.config())
        self.servers = []
        for handler in (server.PrivateHandler, server.PublicHandler):
            handler.lab = lab
            instance = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=instance.serve_forever, daemon=True).start()
            self.servers.append(instance)

    def tearDown(self):
        for instance in self.servers:
            instance.shutdown()
            instance.server_close()
        self.fixture.temporary.cleanup()

    def get(self, index, path, host=None):
        port = self.servers[index].server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_private_server_rejects_foreign_host_headers(self):
        self.assertEqual(self.get(0, "/api/runs")[0], 200)
        self.assertEqual(self.get(0, "/api/runs", host="attacker.example:80")[0], 403)

    def test_public_server_serves_only_public_data(self):
        self.assertEqual(self.get(1, "/api/public")[0], 200)
        for path in ("/api/receipts", "/api/runs", "/api/live", "/static/app.js", "/static/index.html"):
            self.assertEqual(self.get(1, path)[0], 404, path)

    def test_receipt_endpoint(self):
        status, body = self.get(0, f"/api/receipts/{self.fixture.exact_env}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["job"]["id"], self.fixture.exact_env)
        self.assertEqual(self.get(0, "/api/receipts/999999")[0], 404)


if __name__ == "__main__":
    unittest.main()
