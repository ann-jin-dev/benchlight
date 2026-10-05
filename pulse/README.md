# System Pulse

A host agent that samples the workstation every five seconds: CPU, RAM, disk,
network, temperatures, fans, NVIDIA GPUs, processes, Docker experiments, the
gpuq queue, and power. It needs only Python's standard library.

Everything is recorded locally:

| File (under `~/.local/state/system-pulse/`) | Contents |
|---|---|
| `latest.json` | The newest snapshot, read by the lab notebook |
| `history.sqlite3` | Minute averages/min/max for 12 hours, plus temperature alert events |
| `energy.sqlite3` | Per-job GPU and CPU energy for gpuq jobs, and daily machine totals |

Uploading snapshots to a hosted dashboard is optional (see the end of this file).

## Run

```bash
python3 pulse/agent.py --once      # one sample, prints a summary
python3 pulse/agent.py             # the service loop (installed by install.sh)
```

The repository's `install.sh` installs `system-pulse-agent.service` as a
systemd user service. Configuration is optional and lives in
`~/.config/system-pulse/`:

- `profile.json`: the machine's hardware profile (see below). Without it the
  generic profile is used and the outlet estimate is deliberately wide.
- `agent.json` (mode 600): optional settings such as
  `{"allow_sudo": false, "experiments_dir": "/path", "interval_seconds": 5}`.

## Per-job energy

`meter.py` gives every gpuq job an energy record, which the lab notebook turns
into the job's receipt.

- **GPU energy is measured.** NVML exposes a cumulative millijoule counter for
  each card. gpuq reserves cards exclusively, so the counter difference while a
  job holds a card is that job's GPU energy, idle draw included.
- **CPU energy is attributed.** The RAPL package counter is shared by the whole
  machine. Each five-second interval's package energy is split by each job's
  share of busy CPU time, read from the job's own cgroup (`cpu.stat`).
- **Coverage is explicit.** Intervals with a gap longer than 30 s, a reboot or a
  counter reset are not attributed. Each job records how many seconds were
  metered, so a receipt can say what fraction of a run it covers.

The machine's daily totals keep GPU energy that no job claimed (idle cards),
so attributed and unattributed energy can be compared.

## Power model and hardware profiles

The agent reads CPU package energy through Linux RAPL and NVIDIA board power
for each GPU. A whole-machine outlet range is then estimated from a hardware
profile: fixed component ranges (board, memory, peripherals), CPU voltage
regulator loss as a fraction of package power, NVMe activity bands, fan motor
power from measured RPM, and the PSU's published efficiency curve with a
margin. It is an uncalibrated estimate, never a wall-power measurement.

`profiles/x870-dual-rtx5080.json` is the worked example: the workstation this
project was built on, with its PSU's 80 PLUS test report, 11 fans in two
control groups, and display names for its two GPUs. Copy it to
`~/.config/system-pulse/profile.json` and edit it for your parts. Its
`x870-dual-rtx5080-extras/` folder holds the `nct6683 force=1` module options
that board needs for fan RPM readings.

When a USB UPS is configured through Network UPS Tools, the agent reads its
`ups.realpower` or `output.realpower` and shows that real AC value instead of
the estimate. Set `SYSTEM_PULSE_UPS_NAME=name@host` if there are several. UPS
load percent is never treated as watts.

## Permissions (no passwordless sudo needed)

Two readings need more than a normal user:

- **CPU package energy.** `/sys/class/powercap/intel-rapl:0/energy_uj` is
  root-only since the PLATYPUS power side channel (CVE-2020-8694).
  `system/99-rapl-read.rules` makes it readable to a `power` group; the file
  explains the trade-off and the install commands. Without it, CPU watts and
  CPU energy are shown as unavailable.
- **Per-process network rates.** `nethogs` needs packet capture rights:
  `sudo setcap cap_net_admin,cap_net_raw+ep "$(command -v nethogs)"`. This lets
  every local user see per-process traffic. Without it the column is empty.

Hosts that already grant these through sudoers can set `"allow_sudo": true` in
`agent.json` instead.

## GPU queue status

The agent reads `gpuq status --json` every sample and keeps a small,
credential-free projection: worker health, dispatch pause state, active and
waiting jobs, GPU assignments, CPU/RAM reservations against the queue budget,
and scheduler wait reasons. Job commands, environment, logs and private run
paths are never included.

## Twelve-hour history and temperature alerts

`monitoring.py` stores minute averages, minimums, maximums and last readings
for usage, load, network, power, temperatures and fans. Missing readings stay
gaps; zero is used only for real zero readings.

The CPU package, every NVIDIA GPU and the SSD are watched for temperature.
Default warning/critical thresholds are CPU 85/92 °C, GPU 78/84 °C and SSD
60/67 °C. Warnings need 30 continuous seconds, critical alerts 10 seconds, and
an incident clears after 60 seconds at least 3 °C below the warning threshold.
Alerts survive agent restarts. Thresholds can be set in `agent.json` under
`"alerts"` using the same shape as `DEFAULT_SETTINGS` in `monitoring.py`.

## Experiment progress

For Docker Compose experiments made with `docker/new-experiment.sh`, the agent
reads the newest `outputs/progress.json` in each experiment folder. Call the
helper from a training script:

```python
from progress import report_progress

report_progress(step=120, total_steps=1000, metric_name="loss", metric_value=0.38)
```

## Optional hosted dashboard

Set `site_url` and `ingest_token` in `agent.json` to POST every snapshot to
`<site_url>/api/ingest` with an `X-Ingest-Token` header. Extra headers can be
given under `"headers"`. The upload carries the minute history and alert events
not yet acknowledged, so a dashboard can rebuild history after an outage.
