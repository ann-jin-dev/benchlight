#!/usr/bin/env python3
"""Install this user's queue without modifying Docker, drivers or root services."""

import argparse
import os
from pathlib import Path
import subprocess

from gpuqueue.cli import size
from gpuqueue.runtime import checked, gpu_info
from gpuqueue.store import Store, atomic_json, default_root, now


def unit_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=str(default_root()))
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--max-cpus", type=int,
                        help="Shared CPU budget for queued jobs (default: cores minus 4, at most 16)")
    parser.add_argument("--max-memory",
                        help="Shared RAM budget, e.g. 24g (default: RAM minus 6 GiB, at most 24 GiB)")
    args = parser.parse_args()
    os.umask(0o077)
    project = Path(__file__).resolve().parent
    root = Path(args.state_dir).expanduser().resolve()
    checked(["docker", "version", "--format", "{{.Server.Version}}"])
    gpus = gpu_info()
    if not gpus:
        raise SystemExit("No host GPUs found")
    checked(["systemctl", "--user", "show", "--property=Version", "--no-pager"])
    config = root / "config.json"
    store = Store(root)
    store.close()
    if not config.exists():
        memory_total = next(int(line.split()[1]) * 1024 for line in
                            Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:"))
        config_gpus = [{"index": gpu["index"], "uuid": gpu["uuid"], "name": gpu["name"],
                        "memory_total_mib": gpu["memory_total_mib"],
                        "idle_memory_mib": min(gpu["memory_used_mib"], 1024)} for gpu in gpus]
        max_cpus = args.max_cpus or max(1, min(16, (os.cpu_count() or 1) - 4))
        max_memory = (size(args.max_memory) if args.max_memory else
                      min(24, max(1, memory_total // 1024**3 - 6)) * 1024**3)
        if not 1 <= max_cpus <= (os.cpu_count() or 1) or not 1 <= max_memory <= memory_total:
            raise SystemExit("The CPU/RAM budget must fit inside this machine")
        atomic_json(config, {
            "version": 1, "created_at": now(), "gpus": config_gpus,
            "max_cpus": max_cpus, "max_memory_bytes": max_memory,
            "host_memory_headroom_bytes": 2 * 1024**3, "poll_seconds": 2,
            "unmanaged_memory_margin_mib": 512, "unmanaged_utilization_percent": 10,
            "allowed_display_processes": ["gnome-remote-desktop-daemon", "Xorg", "Xwayland", "gnome-shell"],
        })
    executable = project / "gpuq"
    executable.chmod(0o755)
    binary = Path.home() / ".local/bin/gpuq"
    binary.parent.mkdir(parents=True, exist_ok=True)
    if binary.exists() or binary.is_symlink():
        if not binary.is_symlink() or binary.resolve() != executable:
            raise SystemExit(f"Refusing to replace existing executable {binary}")
    else:
        binary.symlink_to(executable)
    unit_path = Path.home() / ".config/systemd/user/gpuq.service"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    content = f"""# Managed by {project}/install.py
[Unit]
Description=Persistent workstation GPU research queue
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={str(project).replace('%', '%%')}
ExecStart={unit_quote(executable)} --state-dir {unit_quote(root)} worker
Environment=PYTHONUNBUFFERED=1
Environment=PATH={str(Path.home() / '.local/bin').replace('%', '%%')}:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=3s
TimeoutStopSec=30s
KillMode=control-group
MemoryMax=512M
CPUQuota=100%

[Install]
WantedBy=default.target
"""
    if unit_path.exists() and not unit_path.read_text().startswith(f"# Managed by {project}/install.py"):
        raise SystemExit(f"Refusing to replace an unrelated service file {unit_path}")
    unit_path.write_text(content)
    checked(["systemctl", "--user", "daemon-reload"])
    if not args.no_start:
        checked(["systemctl", "--user", "enable", "gpuq.service"])
        checked(["systemctl", "--user", "restart", "gpuq.service"])
        checked(["systemctl", "--user", "is-active", "gpuq.service"])
    linger = checked(["loginctl", "show-user", str(os.getuid()), "-p", "Linger"]).strip()
    print(f"Installed {binary}\nState: {root}\n{linger}")
    if linger != "Linger=yes":
        print("Enable persistence across logout/reboot with: loginctl enable-linger " + str(os.getuid()))
    print("Check: gpuq status")


if __name__ == "__main__":
    main()
