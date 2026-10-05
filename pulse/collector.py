"""Read Linux host telemetry without third-party Python dependencies."""

from __future__ import annotations

import csv
import ctypes
from datetime import datetime
import io
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time


from hardware import GENERIC_PROFILE

DEFAULT_EXPERIMENTS = Path(__file__).resolve().parent.parent / "docker" / "experiments"
EXPERIMENTS = Path(os.environ.get("PULSE_EXPERIMENTS_DIR",
                                  os.environ.get("DASHBOARD_EXPERIMENTS", DEFAULT_EXPERIMENTS)))
HWMON = Path("/sys/class/hwmon")
TICKS = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def gpuq_executable() -> str:
    return shutil.which("gpuq") or str(Path.home() / ".local/bin/gpuq")


def fan_motor_range(rpm: float | None, count: int) -> tuple[float, float]:
    """Heuristic DC motor watts for a group of same-control-signal fans.

    At 2000 RPM, this spans 1.2-3 W/fan. The lower endpoint matches the
    published typical draw of a Noctua NF-A12x25 at full speed; the user's
    fan models are unknown, so neither endpoint is a device specification.
    """
    if rpm is None:
        return 0.0, 5.0 * count
    if rpm <= 0:
        return 0.0, 0.2 * count
    speed = min(rpm, 2500) / 2000
    return count * (0.35 + 0.85 * speed ** 3), count * (0.9 + 2.1 * speed ** 3)


def modeled_power_components(cpu_w: float, fans: list[dict], ssd_bps: float | None,
                              profile: dict = GENERIC_PROFILE) -> list[dict]:
    """Itemize estimates, retaining their assumptions for the dashboard."""
    channels = {fan["name"]: fan["rpm"] for fan in fans}

    def fan_group(label: str, numbers: list[int], count: int) -> dict:
        readings = [channels.get(f"Board channel {number}") for number in numbers]
        observed = [rpm for rpm in readings if rpm is not None]
        mean_rpm = round(sum(observed) / len(observed)) if len(observed) == len(numbers) else None
        low, high = fan_motor_range(mean_rpm, count)
        detail = (
            f"{count} motors × {mean_rpm:,} RPM mean; channels {', '.join(map(str, numbers))}"
            if mean_rpm is not None else "RPM input missing; range widened"
        )
        return {"label": label, "low_w": round(low, 1), "high_w": round(high, 1), "detail": detail}

    if ssd_bps is None:
        ssd_w, ssd_detail = (0.3, 7.0), "Activity counter unavailable"
    elif ssd_bps < 128 * 1024:
        ssd_w, ssd_detail = (0.3, 2.0), "NVMe mostly idle"
    elif ssd_bps < 20 * 1024 * 1024:
        ssd_w, ssd_detail = (1.0, 4.0), "NVMe light activity"
    else:
        ssd_w, ssd_detail = (2.0, 7.0), "NVMe sustained activity"

    fixed = [dict(part) for part in profile["fixed_components"]]
    vrm_low, vrm_high = profile["cpu_vrm_loss_fraction"]
    vrm = {"label": "CPU voltage regulators", "low_w": round(cpu_w * vrm_low, 1),
           "high_w": round(cpu_w * vrm_high, 1),
           "detail": f"{vrm_low:.0%}–{vrm_high:.0%} of live CPU package watts"}
    ssd = {"label": profile["ssd"]["label"], "low_w": ssd_w[0], "high_w": ssd_w[1], "detail": ssd_detail}
    groups = [fan_group(group["label"], group["channels"], group["count"]) for group in profile["fan_groups"]]
    return [*fixed[:1], vrm, *fixed[1:], ssd, *groups]


def psu_efficiency(dc_w: float, points=GENERIC_PROFILE["psu"]["efficiency_points"]) -> float:
    """Interpolate the PSU's test curve at a given DC load."""
    if dc_w <= points[0][0]:
        return points[0][1]
    for (low_w, low_eff), (high_w, high_eff) in zip(points, points[1:]):
        if dc_w <= high_w:
            return low_eff + (high_eff - low_eff) * (dc_w - low_w) / (high_w - low_w)
    return points[-1][1]


def read(path: Path | str, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return default


def number(value: str):
    try:
        parsed = float(value.strip().replace(" MiB", "").replace(" W", ""))
        return round(parsed, 2)
    except (ValueError, TypeError):
        return None


def run(args: list[str], timeout: float = 4) -> tuple[str, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return done.stdout, done.stderr if done.returncode else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)


class Collector:
    def __init__(self, profile: dict | None = None, experiments: Path | None = None,
                 allow_sudo: bool = False):
        self.profile = profile or GENERIC_PROFILE
        self.experiments_dir = Path(experiments) if experiments else EXPERIMENTS
        # Legacy hosts read root-only counters through passwordless sudo. The
        # default is a group-readable RAPL file and a capability-enabled nethogs.
        self.allow_sudo = allow_sudo
        self.rapl = Path(self.profile["rapl_domain"])
        self.last_gpuq_status = None
        self.energy = {}
        self.last_time = None
        self.last_cpu = {}
        self.last_process_cpu = {}
        self.last_net = {}
        self.last_rapl = None
        self.last_nvme_sectors = None

    def gpu_queue(self) -> tuple[dict | None, str | None]:
        """Project read-only GPUQ status into a small, credential-free snapshot."""
        output, error = run([gpuq_executable(), "status", "--json"], timeout=3)
        self.last_gpuq_status = None
        if error or not output:
            return None, "GPUQ status unavailable"
        try:
            raw = json.loads(output)
            if not isinstance(raw, dict) or not isinstance(raw.get("jobs"), list):
                raise ValueError("Invalid queue status")
            self.last_gpuq_status = raw
            observation = raw.get("snapshot") or {}
            reservations = raw.get("reservations") or {}
            active_states = {"starting", "running", "cancelling"}

            def stamp(value):
                if not value:
                    return None
                return datetime.fromisoformat(value).timestamp()

            def text(value, limit=240):
                return str(value or "")[:limit]

            jobs = []
            for job in raw["jobs"]:
                spec = job.get("spec") or {}
                state = text(job["status"], 40)
                # Queued/blocked details are scheduler explanations. Runtime
                # commands, environment, logs and private paths are never sent.
                detail = text(job.get("detail")) if state in {"queued", "blocked"} else ""
                jobs.append({
                    "id": int(job["id"]), "name": text(job.get("name")),
                    "project": text(job.get("project"), 120), "status": state,
                    "priority": int(job.get("priority") or 0), "detail": detail,
                    "gpu_count": int(spec.get("gpus") or 0),
                    "cpus": float(spec.get("cpus") or 0),
                    "memory_bytes": int(spec.get("memory_bytes") or 0),
                    "gpu_uuids": list(job.get("allocation") or []),
                    "created_at": stamp(job.get("created_at")),
                    "started_at": stamp(job.get("started_at")),
                })
            active = [job for job in jobs if job["status"] in active_states]
            waiting = sorted(
                (job for job in jobs if job["status"] not in active_states),
                key=lambda job: (-job["priority"], job["id"]),
            )
            worker_state = text(raw.get("worker_state"), 100)
            heartbeat = raw.get("heartbeat_age_seconds")
            verified = worker_state == "running" and heartbeat is not None and not observation.get("error")
            gpus = []
            for gpu in observation.get("gpus") or []:
                uuid = text(gpu["uuid"], 100)
                job_id = reservations.get(uuid)
                blocked = (observation.get("blocked") or {}).get(uuid)
                state = "reserved" if job_id is not None else "unverified" if not verified else "blocked" if blocked else "paused" if raw.get("paused") else "available"
                gpus.append({
                    "index": int(gpu["index"]), "uuid": uuid,
                    "name": text(gpu.get("name"), 120), "state": state,
                    "job_id": int(job_id) if job_id is not None else None,
                    "detail": text(blocked) if verified and blocked else "",
                })
            visible = (active + waiting)[:60]
            return {
                "checked_at": time.time(), "worker_state": worker_state,
                "heartbeat_age_seconds": round(float(heartbeat), 1) if heartbeat is not None else None,
                "paused": bool(raw.get("paused")),
                "host_error": "GPUQ could not verify the host" if observation.get("error") else None,
                "counts": {text(key, 40): int(value) for key, value in (raw.get("counts") or {}).items()},
                "budgets": {
                    "cpus": float((raw.get("budgets") or {}).get("cpus") or 0),
                    "memory_bytes": int((raw.get("budgets") or {}).get("memory_bytes") or 0),
                },
                "reserved": {
                    "cpus": sum(job["cpus"] for job in active),
                    "memory_bytes": sum(job["memory_bytes"] for job in active),
                },
                "gpus": gpus, "jobs": visible, "total_jobs": len(jobs),
                "jobs_truncated": len(jobs) - len(visible),
            }, None
        except (ValueError, TypeError, KeyError, AttributeError):
            return None, "GPUQ returned an unreadable status"

    def nvme_bytes_per_second(self, elapsed: float) -> float | None:
        """Use Linux NVMe read/write sector deltas to choose an activity band."""
        fields = read(f"/sys/block/{self.profile['ssd']['device']}/stat").split()
        try:
            sectors = int(fields[2]) + int(fields[6])
        except (IndexError, ValueError):
            self.last_nvme_sectors = None
            return None
        previous = self.last_nvme_sectors
        self.last_nvme_sectors = sectors
        if previous is None or elapsed <= 0:
            return None
        return max(0, sectors - previous) * 512 / elapsed

    def cpu(self):
        current = {}
        for line in read("/proc/stat").splitlines():
            bits = line.split()
            if not bits or not bits[0].startswith("cpu"):
                continue
            try:
                values = [int(x) for x in bits[1:]]
                current[bits[0]] = (sum(values), values[3] + values[4])
            except (ValueError, IndexError):
                continue
        usages = {}
        for key, (total, idle) in current.items():
            previous = self.last_cpu.get(key)
            if previous:
                span = total - previous[0]
                usages[key] = round(100 * (1 - (idle - previous[1]) / span), 1) if span > 0 else 0
        self.last_cpu = current
        return {"total": usages.get("cpu", 0), "cores": [v for k, v in usages.items() if k != "cpu"]}

    def memory(self):
        values = {}
        for line in read("/proc/meminfo").splitlines():
            key, _, raw = line.partition(":")
            try:
                values[key] = int(raw.split()[0]) * 1024
            except (ValueError, IndexError):
                pass
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        swap_total = values.get("SwapTotal", 0)
        return {
            "used": max(total - available, 0), "total": total,
            "percent": round(100 * (total - available) / total, 1) if total else 0,
            "swap_used": max(swap_total - values.get("SwapFree", 0), 0),
            "swap_total": swap_total,
        }

    def disk(self):
        try:
            stat = os.statvfs("/")
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            return {"used": total - free, "total": total, "percent": round(100 * (total-free) / total, 1)}
        except (OSError, ZeroDivisionError):
            return {"used": 0, "total": 0, "percent": 0}

    def network(self, elapsed: float):
        totals = {}
        interfaces = []
        for line in read("/proc/net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            name, raw = line.split(":", 1)
            name = name.strip()
            parts = raw.split()
            if name == "lo" or len(parts) < 9:
                continue
            try:
                received, sent = int(parts[0]), int(parts[8])
            except ValueError:
                continue
            totals[name] = (received, sent)
            old = self.last_net.get(name)
            interfaces.append({
                "name": name,
                "down_bps": max(0, round((received - old[0]) / elapsed)) if old and elapsed else 0,
                "up_bps": max(0, round((sent - old[1]) / elapsed)) if old and elapsed else 0,
            })
        self.last_net = totals
        # Physical and wireless interfaces are primary; container bridges remain visible separately.
        primary = [x for x in interfaces if not x["name"].startswith(("veth", "br-", "docker", "tailscale"))]
        return {
            "down_bps": sum(x["down_bps"] for x in primary),
            "up_bps": sum(x["up_bps"] for x in primary),
            "interfaces": sorted(interfaces, key=lambda x: x["down_bps"] + x["up_bps"], reverse=True)[:8],
        }

    def sensors(self):
        temperatures, fans = [], []
        device_counts = {}
        devices = sorted(HWMON.glob("hwmon*"), key=lambda hw: str(hw.resolve()))
        chip_totals = {}
        for hw in devices:
            chip = read(hw / "name", hw.name)
            chip_totals[chip] = chip_totals.get(chip, 0) + 1
        for hw in devices:
            chip = read(hw / "name", hw.name)
            device_counts[chip] = device_counts.get(chip, 0) + 1
            device_number = device_counts[chip]
            for path in hw.glob("temp*_input"):
                value = number(read(path))
                if value is None or not -20 <= value / 1000 <= 130:
                    continue
                raw_label = read(path.with_name(path.name.replace("_input", "_label")))
                input_number = path.stem.split("_")[0].removeprefix("temp")
                if chip == "k10temp":
                    label = "CPU package" if raw_label == "Tctl" else raw_label.replace("Tccd", "CPU core group ")
                elif chip == "nvme":
                    drive = f"SSD {device_number}" if chip_totals[chip] > 1 else "SSD"
                    label = drive if raw_label == "Composite" else f"{drive} {raw_label.lower() or 'sensor ' + input_number}"
                elif chip == "spd5118":
                    label = f"Memory module {device_number}"
                elif chip == "amdgpu":
                    label = "Integrated GPU"
                elif chip == self.profile["board_fan_chip"]:
                    label = "CPU board sensor" if raw_label.startswith("AMD TSI") else f"Motherboard sensor {input_number}"
                elif raw_label in ("MAC Temperature", "PHY Temperature"):
                    label = "Ethernet " + raw_label.split()[0]
                elif chip.startswith("r8169"):
                    label = "Ethernet controller"
                else:
                    label = raw_label or f"{chip} sensor {input_number}"
                temperatures.append({"name": label, "chip": chip, "celsius": round(value / 1000, 1)})
            for path in hw.glob("fan*_input"):
                value = number(read(path))
                if value is None:
                    continue
                fan_number = path.stem.split("_")[0].removeprefix("fan")
                label = read(path.with_name(path.name.replace("_input", "_label"))) or (
                    f"Board channel {fan_number}" if chip == self.profile["board_fan_chip"]
                    else f"{chip} fan {fan_number}"
                )
                pwm = number(read(hw / f"pwm{fan_number}"))
                fans.append({
                    "name": label, "rpm": round(value),
                    "pwm_percent": round(100 * pwm / 255) if pwm is not None and 0 <= pwm <= 255 else None,
                })
        seen = set()
        selected = []
        for temp in sorted(temperatures, key=lambda x: (x["name"] != "CPU package", not x["name"].startswith("SSD"), x["name"])):
            if temp["name"] not in seen:
                selected.append(temp)
                seen.add(temp["name"])
        return {"temperatures": selected[:20], "fans": sorted(fans, key=lambda x: x["name"])}

    def gpus(self):
        if not shutil.which("nvidia-smi"):
            return [], {}, "nvidia-smi is not installed"
        fields = "index,uuid,name,temperature.gpu,fan.speed,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw"
        out, err = run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"])
        if err or not out.strip():
            return [], {}, (err.strip() or "NVIDIA driver unavailable")[:180]
        gpus = []
        for row in csv.reader(io.StringIO(out)):
            if len(row) != 10:
                continue
            index, uuid, name, temp, fan, util, mem_util, used, total, power = [x.strip() for x in row]
            gpus.append({
                "index": int(index), "uuid": uuid, "name": name,
                "temperature": number(temp), "fan_percent": number(fan),
                "fan_channels": [],
                "utilization": number(util), "memory_utilization": number(mem_util),
                "vram_used": round((number(used) or 0) * 1024 * 1024),
                "vram_total": round((number(total) or 0) * 1024 * 1024),
                "power_w": number(power),
            })
        self.gpu_fan_channels(gpus)
        process_gpu = {}
        apps, _ = run(["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory,gpu_uuid", "--format=csv,noheader,nounits"])
        for row in csv.reader(io.StringIO(apps)):
            if len(row) != 3:
                continue
            try:
                pid = int(row[0].strip())
            except ValueError:
                continue
            usage = process_gpu.setdefault(pid, {"gpu_percent": None, "vram_bytes": 0, "gpu_indices": []})
            usage["vram_bytes"] += round((number(row[1]) or 0) * 1024 * 1024)
            usage["gpu_indices"].extend([g["index"] for g in gpus if g["uuid"] == row[2].strip()])
        pmon, _ = run(["nvidia-smi", "pmon", "-c", "1", "-s", "um"], timeout=3)
        pmon_vram = {}
        for line in pmon.splitlines():
            parts = line.split()
            if not parts or parts[0] == "#" or len(parts) < 5:
                continue
            try:
                pid = int(parts[1])
                gpu_index = int(parts[0])
            except ValueError:
                continue
            usage = process_gpu.setdefault(pid, {"gpu_percent": None, "vram_bytes": 0, "gpu_indices": []})
            if parts[3] != "-":
                usage["gpu_percent"] = max(usage["gpu_percent"] or 0, number(parts[3]) or 0)
            if len(parts) > 9 and parts[9] != "-":
                pmon_vram[pid] = pmon_vram.get(pid, 0) + round((number(parts[9]) or 0) * 1024 * 1024)
            if gpu_index not in usage["gpu_indices"]:
                usage["gpu_indices"].append(gpu_index)
        for pid, vram in pmon_vram.items():
            if process_gpu[pid]["vram_bytes"] == 0:
                process_gpu[pid]["vram_bytes"] = vram
        return gpus, process_gpu, None

    def ups_realpower(self):
        """Use only a UPS-reported real-watt value, never load percent as watts."""
        if not shutil.which("upsc"):
            return None
        ups_name = os.environ.get("SYSTEM_PULSE_UPS_NAME", "").strip()
        if not ups_name:
            available, _ = run(["upsc", "-l"], timeout=2)
            names = [line.strip() for line in available.splitlines() if line.strip()]
            if len(names) != 1:
                return None
            ups_name = names[0]
        target = ups_name if "@" in ups_name else ups_name + "@localhost"
        for field in ("ups.realpower", "output.realpower"):
            raw, error = run(["upsc", target, field], timeout=2)
            watts = number(raw)
            if not error and watts is not None and 0 <= watts <= 3000:
                return watts
        return None

    def power(self, gpus: list[dict], gpu_error: str | None, fans: list[dict], ssd_bps: float | None):
        """Sample CPU package and GPU board draw; model, never claim, wall power."""
        raw_energy = read(self.rapl / "energy_uj")
        if not raw_energy and self.allow_sudo:
            raw_energy, _ = run(["sudo", "-n", "cat", str(self.rapl / "energy_uj")], timeout=2)
        try:
            energy = int(raw_energy.strip())
            maximum = int(read(self.rapl / "max_energy_range_uj"))
        except ValueError:
            energy, maximum = None, None
        self.energy["rapl_uj"], self.energy["rapl_max_uj"] = energy, maximum
        sample_time = time.monotonic()
        cpu_w = None
        if energy is not None and maximum and self.last_rapl is not None:
            previous_time, previous_energy = self.last_rapl
            elapsed = sample_time - previous_time
            delta = (energy - previous_energy) % maximum
            if elapsed > 0 and delta < maximum / 2:
                value = delta / 1_000_000 / elapsed
                if 0 <= value <= 500:
                    cpu_w = round(value, 1)
        self.last_rapl = (sample_time, energy) if energy is not None else None

        gpu_w = round(sum(g["power_w"] for g in gpus if g["power_w"] is not None), 1)
        gpu_complete = not gpu_error and bool(gpus) and all(g["power_w"] is not None for g in gpus)
        monitored_w = round(cpu_w + gpu_w, 1) if cpu_w is not None and gpu_complete else None
        # Illustrative wall-power range, not guaranteed bounds or a meter reading.
        # The efficiency margin allows for unit, temperature, and load-mix variation.
        estimate_low_w = estimate_high_w = None
        psu_loss_low_w = psu_loss_high_w = None
        components = []
        other_dc_low_w = other_dc_high_w = None
        if monitored_w is not None:
            components = modeled_power_components(cpu_w, fans, ssd_bps, self.profile)
            other_dc_low_w = round(sum(part["low_w"] for part in components), 1)
            other_dc_high_w = round(sum(part["high_w"] for part in components), 1)
            low_dc = monitored_w + other_dc_low_w
            high_dc = monitored_w + other_dc_high_w
            psu = self.profile["psu"]
            low_efficiency = min(0.99, psu_efficiency(low_dc, psu["efficiency_points"]) + psu["margin"])
            high_efficiency = max(0.5, psu_efficiency(high_dc, psu["efficiency_points"]) - psu["margin"])
            estimate_low_w = math.floor(low_dc / low_efficiency / 5) * 5
            estimate_high_w = math.ceil(high_dc / high_efficiency / 5) * 5
            psu_loss_low_w = round(low_dc / low_efficiency - low_dc, 1)
            psu_loss_high_w = round(high_dc / high_efficiency - high_dc, 1)
        ups_w = self.ups_realpower()
        return {
            "cpu_package_w": cpu_w,
            "gpu_total_w": gpu_w if gpu_complete else None,
            "monitored_w": monitored_w,
            "other_dc_low_w": other_dc_low_w,
            "other_dc_high_w": other_dc_high_w,
            "modeled_components": components,
            "psu_loss_low_w": psu_loss_low_w,
            "psu_loss_high_w": psu_loss_high_w,
            "estimate_low_w": estimate_low_w,
            "estimate_high_w": estimate_high_w,
            "measured_ac_w": ups_w,
            "measured_source": "UPS output" if ups_w is not None else None,
            "profile": self.profile["name"], "psu": self.profile["psu"]["label"],
        }

    def gpu_fan_channels(self, gpus):
        """Read NVML fan channels and energy counters, without changing any controls."""
        class FanSpeedInfo(ctypes.Structure):
            _fields_ = [("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint)]

        try:
            nvml = ctypes.CDLL("libnvidia-ml.so.1")
            nvml.nvmlInit_v2.restype = ctypes.c_int
            nvml.nvmlDeviceGetHandleByUUID.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
            nvml.nvmlDeviceGetHandleByUUID.restype = ctypes.c_int
            nvml.nvmlDeviceGetNumFans.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            nvml.nvmlDeviceGetNumFans.restype = ctypes.c_int
            nvml.nvmlDeviceGetFanSpeed_v2.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
            nvml.nvmlDeviceGetFanSpeed_v2.restype = ctypes.c_int
            nvml.nvmlDeviceGetFanSpeedRPM.argtypes = [ctypes.c_void_p, ctypes.POINTER(FanSpeedInfo)]
            nvml.nvmlDeviceGetFanSpeedRPM.restype = ctypes.c_int
            nvml.nvmlDeviceGetTotalEnergyConsumption.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulonglong)]
            nvml.nvmlDeviceGetTotalEnergyConsumption.restype = ctypes.c_int
            if nvml.nvmlInit_v2() != 0:
                return
        except (OSError, AttributeError):
            return
        try:
            for gpu in gpus:
                handle = ctypes.c_void_p()
                if nvml.nvmlDeviceGetHandleByUUID(gpu["uuid"].encode(), ctypes.byref(handle)) != 0:
                    continue
                # Millijoules since the driver loaded; exact per-card energy for receipts.
                energy = ctypes.c_ulonglong()
                if nvml.nvmlDeviceGetTotalEnergyConsumption(handle, ctypes.byref(energy)) == 0:
                    gpu["energy_mj"] = energy.value
                count = ctypes.c_uint()
                if nvml.nvmlDeviceGetNumFans(handle, ctypes.byref(count)) != 0:
                    continue
                for index in range(min(count.value, 8)):
                    percent = ctypes.c_uint()
                    percent_ok = nvml.nvmlDeviceGetFanSpeed_v2(handle, index, ctypes.byref(percent)) == 0
                    info = FanSpeedInfo(ctypes.sizeof(FanSpeedInfo) | (1 << 24), index, 0)
                    rpm_ok = nvml.nvmlDeviceGetFanSpeedRPM(handle, ctypes.byref(info)) == 0
                    gpu["fan_channels"].append({
                        "index": index + 1,
                        "percent": percent.value if percent_ok else None,
                        "rpm": info.speed if rpm_ok else None,
                    })
        finally:
            nvml.nvmlShutdown()

    def process_network(self):
        if not shutil.which("nethogs"):
            return {}, "nethogs is not installed"
        command = ["nethogs", "-t", "-d", "1", "-c", "1", "-v", "0"]
        out, err = run(["sudo", "-n", *command] if self.allow_sudo else command, timeout=5)
        if err and not out:
            return {}, "nethogs needs network capture capabilities (see pulse/README.md)"
        rates = {}
        for line in out.splitlines():
            fields = line.split("\t")
            if len(fields) != 3:
                continue
            try:
                pid = int(fields[0].rsplit("/", 2)[-2])
                if pid <= 0:
                    continue
                sent = float(fields[1]) * 1024
                received = float(fields[2]) * 1024
            except (ValueError, IndexError):
                continue
            old = rates.get(pid, {"up_bps": 0, "down_bps": 0})
            rates[pid] = {"up_bps": round(old["up_bps"] + sent), "down_bps": round(old["down_bps"] + received)}
        return rates, None

    def processes(self, elapsed: float, gpu_processes: dict, net_processes: dict):
        current = {}
        rows = []
        mem_total = self.memory()["total"]
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            pid = int(proc.name)
            stat = read(proc / "stat")
            end = stat.rfind(")")
            if end < 0:
                continue
            fields = stat[end + 2:].split()
            if len(fields) < 22:
                continue
            try:
                ticks = int(fields[11]) + int(fields[12])
                rss = int(fields[21]) * PAGE_SIZE
                state = fields[0]
            except ValueError:
                continue
            current[pid] = ticks
            prev = self.last_process_cpu.get(pid)
            cpu = max(0, round((ticks - prev) * 100 / TICKS / elapsed, 1)) if prev is not None and elapsed else 0
            cmdline = read(proc / "cmdline").replace("\x00", " ").strip()
            command = stat[stat.find("(") + 1:end]
            if cmdline:
                command = Path(cmdline.split(" ", 1)[0]).name or command
            net = net_processes.get(pid, {})
            gpu = gpu_processes.get(pid, {})
            rows.append({
                "pid": pid, "name": command[:80], "state": state,
                "cpu_percent": cpu, "ram_bytes": rss,
                "ram_percent": round(100 * rss / mem_total, 1) if mem_total else 0,
                "net_up_bps": net.get("up_bps", 0), "net_down_bps": net.get("down_bps", 0),
                "gpu_percent": gpu.get("gpu_percent"), "vram_bytes": gpu.get("vram_bytes", 0),
                "gpu_indices": gpu.get("gpu_indices", []),
            })
        self.last_process_cpu = current
        rows.sort(key=lambda x: (x["cpu_percent"], x["ram_bytes"]), reverse=True)
        return rows[:120]

    def containers(self):
        if not shutil.which("docker"):
            return {}, "Docker is not installed"
        out, err = run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=4)
        if err and not out:
            # The service may start before a fresh docker group membership applies.
            out, err = run(["sg", "docker", "-c", "docker ps -a --format '{{json .}}'"], timeout=4)
        if err and not out:
            return {}, "Docker access unavailable"
        containers = {}
        for line in out.splitlines():
            try:
                record = json.loads(line)
                name = record.get("Names", "")
                containers[name] = {"name": name, "status": record.get("Status", ""), "state": record.get("State", "")}
            except json.JSONDecodeError:
                continue
        return containers, None

    def experiments(self, containers: dict):
        result = []
        if not self.experiments_dir.exists():
            return result
        for folder in sorted(self.experiments_dir.iterdir()):
            if not folder.is_dir() or not (folder / "compose.yaml").exists():
                continue
            candidates = [folder / "outputs" / "progress.json"]
            candidates.extend((folder / "outputs").glob("*/progress.json"))
            candidates = [x for x in candidates if x.is_file()]
            progress = {}
            last_update = None
            if candidates:
                latest = max(candidates, key=lambda x: x.stat().st_mtime)
                try:
                    progress = json.loads(latest.read_text())
                    if not isinstance(progress, dict):
                        progress = {}
                    last_update = latest.stat().st_mtime
                except (OSError, ValueError):
                    progress = {}
            services = [c for name, c in containers.items() if name.startswith(folder.name + "-")]
            running = any(c["state"].lower() == "running" for c in services)
            step = progress.get("step")
            total = progress.get("total_steps")
            try:
                pct = round(min(100, max(0, 100 * float(step) / float(total))), 1) if total else None
            except (ValueError, TypeError, ZeroDivisionError):
                pct = None
            status = progress.get("status") if isinstance(progress.get("status"), str) else None
            if not status:
                status = "running" if running else "idle"
            elif status == "running" and not running and last_update and time.time() - last_update > 60:
                status = "stale"
            result.append({
                "name": folder.name, "status": status[:30], "running": running,
                "containers": services, "step": step, "total_steps": total,
                "epoch": progress.get("epoch"), "total_epochs": progress.get("total_epochs"),
                "percent": pct, "metric_name": str(progress.get("metric_name", ""))[:40],
                "metric_value": progress.get("metric_value"),
                "message": str(progress.get("message", ""))[:160],
                "updated_at": last_update,
            })
        return result

    def collect(self):
        now = time.monotonic()
        elapsed = now - self.last_time if self.last_time else 0
        self.last_time = now
        gpu, gpu_processes, gpu_error = self.gpus()
        sensor_data = self.sensors()
        ssd_bps = self.nvme_bytes_per_second(elapsed)
        power = self.power(gpu, gpu_error, sensor_data["fans"], ssd_bps)
        net_processes, net_error = self.process_network()
        containers, docker_error = self.containers()
        gpu_queue, queue_error = self.gpu_queue()
        cpu = self.cpu()
        memory = self.memory()
        disk = self.disk()
        network = self.network(elapsed)
        processes = self.processes(elapsed, gpu_processes, net_processes)
        try:
            load = list(os.getloadavg())
        except OSError:
            load = [0, 0, 0]
        uptime = number(read("/proc/uptime").split(" ")[0]) or 0
        warnings = []
        if gpu_error:
            warnings.append("NVIDIA metrics unavailable")
        if net_error:
            warnings.append("Process network rates unavailable")
        if docker_error:
            warnings.append("Docker status unavailable")
        if queue_error:
            warnings.append("GPUQ status unavailable")
        return {
            "timestamp": time.time(), "hostname": socket.gethostname(), "uptime_seconds": uptime,
            "cpu": cpu, "load": load, "memory": memory, "disk": disk,
            "network": network, "sensors": sensor_data, "gpus": gpu,
            "power": power,
            "gpu_queue": gpu_queue,
            "processes": processes, "experiments": self.experiments(containers),
            "availability": {"gpu": gpu_error, "process_network": net_error, "docker": docker_error, "gpuq": queue_error},
            "warnings": warnings,
        }
