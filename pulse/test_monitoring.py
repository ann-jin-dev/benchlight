import copy
from pathlib import Path
import tempfile
import unittest

from monitoring import DEFAULT_SETTINGS, Monitor, advance_alert


class TemperatureAlerts(unittest.TestCase):
    def test_short_spike_does_not_notify_and_sustained_heat_does(self):
        state = {"level": "normal"}
        rule = DEFAULT_SETTINGS["rules"]["cpu"]
        for second, value in ((0, 86), (5, 86), (10, 70)):
            self.assertIsNone(advance_alert(state, value, rule, DEFAULT_SETTINGS, second * 1000))
        for second in range(15, 46, 5):
            transition = advance_alert(state, 86, rule, DEFAULT_SETTINGS, second * 1000)
        self.assertEqual(transition, "warning")
        self.assertIsNone(advance_alert(state, 86, rule, DEFAULT_SETTINGS, 50_000))

    def test_escalation_and_hysteresis_recovery(self):
        state = {"level": "warning"}
        rule = DEFAULT_SETTINGS["rules"]["gpu"]
        self.assertIsNone(advance_alert(state, 85, rule, DEFAULT_SETTINGS, 0))
        self.assertIsNone(advance_alert(state, 85, rule, DEFAULT_SETTINGS, 5000))
        self.assertEqual(advance_alert(state, 85, rule, DEFAULT_SETTINGS, 10_000), "critical")
        for second in range(15, 86, 5):
            self.assertIsNone(advance_alert(state, 77, rule, DEFAULT_SETTINGS, second * 1000))
        for second in range(90, 151, 5):
            transition = advance_alert(state, 74, rule, DEFAULT_SETTINGS, second * 1000)
        self.assertEqual(transition, "recovery")
        self.assertEqual(state["level"], "normal")

    def test_gap_does_not_count_as_continuous_heat(self):
        state = {"level": "normal"}
        rule = DEFAULT_SETTINGS["rules"]["cpu"]
        advance_alert(state, 86, rule, DEFAULT_SETTINGS, 0)
        self.assertIsNone(advance_alert(state, 86, rule, DEFAULT_SETTINGS, 40_000))

    def test_minute_history_preserves_spikes_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.sqlite3"
            monitor = Monitor(path)
            for second, temp in ((120, 40), (125, 90), (130, 40)):
                snapshot = {"timestamp": second, "cpu": {"total": 10}, "sensors": {"temperatures": [{"name": "CPU package", "celsius": temp}]}}
                monitor.observe(snapshot)
            bucket = snapshot["history_batch"][0]
            self.assertEqual(bucket["values"]["temperature:CPU package"][1:3], [40, 90])
            self.assertEqual(bucket["samples"], 3)
            monitor.db.close()
            restarted = Monitor(path)
            self.assertEqual(restarted.db.execute("SELECT samples FROM buckets").fetchone()[0], 3)
            restarted.acknowledge(snapshot)
            self.assertEqual(restarted.db.execute("SELECT revision-sent_revision FROM buckets").fetchone()[0], 0)

    def test_limits_cannot_be_silently_increased(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = Monitor(Path(folder) / "history.sqlite3")
            invalid = copy.deepcopy(DEFAULT_SETTINGS)
            invalid["rules"]["cpu"]["critical"] = 100
            monitor.apply_settings(invalid)
            self.assertEqual(monitor.settings["rules"]["cpu"]["critical"], 92)


if __name__ == "__main__":
    unittest.main()
