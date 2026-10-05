import tempfile
import unittest
from pathlib import Path

from meter import EnergyMeter, host_busy_usec


def reading(t, gpu_a, gpu_b, rapl=None, busy=None, jobs=(), boot="boot-1"):
    return {"time": t, "boot_id": boot, "gpu_mj": {"GPU-a": gpu_a, "GPU-b": gpu_b},
            "rapl_uj": rapl, "rapl_max_uj": 2**40 if rapl is not None else None,
            "host_busy_usec": busy, "jobs": list(jobs)}


def job(job_id, gpus, cpu_usec=None):
    return {"id": job_id, "instance": "queue-1", "project": "demo", "gpus": gpus, "cpu_usec": cpu_usec}


class MeterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.meter = EnergyMeter(Path(self.temporary.name) / "energy.sqlite3")

    def tearDown(self):
        self.meter.close()
        self.temporary.cleanup()

    def row(self, job_id):
        return self.meter.db.execute(
            "SELECT gpu_mj, cpu_mj, cpu_seconds, gpu_metered_seconds, cpu_metered_seconds "
            "FROM jobs WHERE queue_instance='queue-1' AND job_id=?", (job_id,)).fetchone()

    def test_exclusive_card_energy_belongs_to_the_job_holding_it(self):
        self.meter.observe(reading(0, 1_000, 5_000, jobs=[job(7, ["GPU-a"])]))
        self.meter.observe(reading(5, 1_000 + 900_000, 5_000 + 100_000, jobs=[job(7, ["GPU-a"])]))
        gpu_mj, _, _, metered, _ = self.row(7)
        self.assertEqual(gpu_mj, 900_000)
        self.assertEqual(metered, 5)
        day = self.meter.db.execute("SELECT gpu_mj, job_gpu_mj FROM days").fetchone()
        self.assertEqual(day, (1_000_000, 900_000))  # the idle card stays unattributed

    def test_cpu_package_energy_is_split_by_busy_cpu_time(self):
        start = reading(0, 0, 0, rapl=0, busy=0, jobs=[job(3, [], cpu_usec=0)])
        later = reading(5, 0, 0, rapl=100_000_000, busy=4_000_000, jobs=[job(3, [], cpu_usec=1_000_000)])
        self.meter.observe(start)
        summary = self.meter.observe(later)
        _, cpu_mj, cpu_seconds, _, cpu_metered = self.row(3)
        self.assertAlmostEqual(cpu_mj, 25_000)  # 1 s of 4 busy seconds of a 100 J interval
        self.assertEqual(cpu_seconds, 1)
        self.assertEqual(cpu_metered, 5)
        self.assertTrue(summary["cpu_measured"])

    def test_missing_rapl_leaves_cpu_energy_unmetered(self):
        self.meter.observe(reading(0, 0, 0, busy=0, jobs=[job(3, [], cpu_usec=0)]))
        self.meter.observe(reading(5, 0, 0, busy=4_000_000, jobs=[job(3, [], cpu_usec=1_000_000)]))
        _, cpu_mj, cpu_seconds, _, cpu_metered = self.row(3)
        self.assertEqual((cpu_mj, cpu_metered), (0, 0))
        self.assertEqual(cpu_seconds, 1)

    def test_gaps_reboots_and_counter_resets_are_not_attributed(self):
        held = [job(1, ["GPU-a"])]
        self.meter.observe(reading(0, 1_000_000, 0, jobs=held))
        self.meter.observe(reading(100, 2_000_000, 0, jobs=held))  # 100 s gap
        self.meter.observe(reading(105, 3_000_000, 0, jobs=held, boot="boot-2"))
        self.meter.observe(reading(110, 10, 0, jobs=held, boot="boot-2"))  # driver reload
        row = self.row(1)
        self.assertTrue(row is None or (row[0] == 0 and row[3] == 0))

    def test_first_sighting_starts_metering_without_attribution(self):
        self.meter.observe(reading(0, 0, 0))
        self.meter.observe(reading(5, 500_000, 0, jobs=[job(9, ["GPU-a"])]))
        self.assertIsNone(self.row(9))
        self.meter.observe(reading(10, 900_000, 0, jobs=[job(9, ["GPU-a"])]))
        self.assertEqual(self.row(9)[0], 400_000)

    def test_summary_reports_watt_hours(self):
        held = [job(5, ["GPU-a", "GPU-b"])]
        self.meter.observe(reading(0, 0, 0, jobs=held))
        summary = self.meter.observe(reading(10, 1_800_000, 1_800_000, jobs=held))
        self.assertEqual(summary["jobs"]["5"]["gpu_wh"], 1.0)
        self.assertEqual(summary["today"]["gpu_wh"], 1.0)

    def test_cpu_only_job_without_rapl_is_not_metered(self):
        self.meter.observe(reading(0, 0, 0, busy=0, jobs=[job(4, [], cpu_usec=0)]))
        self.meter.observe(reading(5, 0, 0, busy=1_000_000, jobs=[job(4, [], cpu_usec=500_000)]))
        _, _, _, gpu_metered, cpu_metered = self.row(4)
        self.assertEqual((gpu_metered, cpu_metered), (0, 0))
        minutes = self.meter.db.execute("SELECT COUNT(*) FROM job_minutes").fetchone()[0]
        self.assertEqual(minutes, 0)

    def test_unmetered_intervals_do_not_dilute_minute_power(self):
        held = [job(2, ["GPU-a"])]
        self.meter.observe(reading(60, 0, 0, jobs=held))
        self.meter.observe(reading(65, 1_000_000, 0, jobs=held))  # 1 kJ in 5 s
        missing = dict(reading(70, 0, 0, jobs=held), gpu_mj={})   # NVML unavailable
        self.meter.observe(missing)
        gpu_mj, seconds = self.meter.db.execute(
            "SELECT gpu_mj, seconds FROM job_minutes WHERE job_id=2").fetchone()
        self.assertEqual(gpu_mj / 1000 / seconds, 200.0)

    def test_a_new_queue_instance_does_not_inherit_old_readings(self):
        old = dict(job(5, ["GPU-a"]), instance="queue-old")
        self.meter.observe(reading(0, 0, 0, jobs=[old]))
        self.meter.observe(reading(5, 900_000, 0, jobs=[job(5, ["GPU-a"])]))
        self.assertIsNone(self.row(5))

    def test_zero_rapl_range_is_ignored(self):
        start = dict(reading(0, 0, 0, rapl=10, busy=0, jobs=[job(3, [], cpu_usec=0)]), rapl_max_uj=0)
        later = dict(reading(5, 0, 0, rapl=20, busy=10, jobs=[job(3, [], cpu_usec=5)]), rapl_max_uj=0)
        self.meter.observe(start)
        self.meter.observe(later)  # must not raise ZeroDivisionError

    def test_host_busy_time_excludes_idle_and_iowait(self):
        stat = "cpu  100 0 50 1000 500 5 5 0 0 0\ncpu0 1 1 1 1 1 1 1 1 0 0\n"
        ticks = 160
        import meter
        self.assertEqual(host_busy_usec(stat), ticks * 1_000_000 // meter.TICKS)


if __name__ == "__main__":
    unittest.main()
