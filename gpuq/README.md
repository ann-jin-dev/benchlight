# gpuq: one GPU queue for people and AI agents on one workstation

`gpuq` installs to `~/.local/bin/gpuq`. One persistent systemd user service
coordinates every NVIDIA GPU in the machine across all of one account's projects,
whether a person or a coding agent submits the work. It needs Python's standard
library, an existing Docker/NVIDIA setup, and systemd; there is no additional
Python environment, server, port, or cloud service.

## Submit an experiment

From a project's source directory, submit its existing Docker image and command:

```bash
gpuq submit --project my-project --image my-project:version \
  --gpus 1 --cpus 4 --memory 8g --snapshot \
  --mount /absolute/path/to/data:/workspace/data:ro \
  -- python train.py --output /workspace/run/outputs
```

Replace the image, dataset path, and training arguments with that project's
actual values. `submit` prints the job ID and returns immediately. Build or pull
images before submission. Tags resolve to an immutable image ID when the job
starts; use an image ID/digest at submission when the runtime must already be
frozen. Each run records the resolved image ID.

Docker mounts the submitted directory read-only at `/workspace/code`, uses it
as the working directory, and gives each job a writable `/workspace/run` with
`outputs/` and `tmp/`. `/workspace/cache` is a persistent cache per project; use
`--cache-dir /absolute/path` to reuse a project's established cache. Extra mounts
default to read-only; explicitly suffix `:rw` for writable mounts. Code, run and
cache mount destinations are reserved. The container runs as your Linux UID/GID.

`--snapshot` freezes tracked and unignored Git source before enqueueing, including
dirty changes and untracked source. It records file SHA-256 hashes. Snapshots are
limited to 256 MiB and 20,000 files: keep datasets, checkpoints, environments and
outputs out of the source snapshot and mount them separately. Without this flag,
the queued command sees the directory's contents when it starts. An existing
frozen source directory can also be passed through `--cwd`.

Native foreground commands are supported too. The CLI resolves their executable
from the submitting shell, so an activated virtual environment works:

```bash
cd ~/projects/my-project
gpuq submit --project my-project --gpus 1 --cpus 4 --memory 8g \
  -- /absolute/path/to/venv/bin/python train.py
```

For native jobs, write results to `os.environ["GPUQ_OUTPUT_DIR"]`. Native commands
run in independently supervised systemd user services. Their GPU visibility uses
`CUDA_VISIBLE_DEVICES`; Docker jobs also have device access restricted by Docker.
The queue preserves the executable's symlink path so Python can locate its
virtual environment. With `--snapshot`, executables included in the frozen
source are remapped into that snapshot; ignored virtual environments remain at
their original paths.
Submit Docker work with `--image`. A native job that invokes an unmodified Docker
launcher creates containers outside that native service's supervision and needs
an explicit queue adapter before it can provide reliable cancellation and GPU
assignment. Preserve each project's scientific configuration and launch protocol
when adapting its command and mounts.

## Scheduling

- GPU reservations are exclusive and use stable physical UUIDs. A free card with
  less memory in use is chosen first, usually the card not driving a display.
- One-GPU jobs run side by side. `--gpus 2` reserves two cards atomically.
  For a supported distributed training script, the command can be
  `torchrun --standalone --nproc-per-node=2 train_ddp.py`. The script implements
  distributed training; requesting two devices does not convert it to DDP.
- `--gpu 0` or `--gpu 1` pins a one-GPU request. `--gpu 0,1 --gpus 2` pins both.
  Full GPU UUIDs are also accepted. Inside a one-GPU Docker job use `cuda:0`,
  regardless of its physical index.
- `--gpus 0` schedules CPU preparation or analysis through the same resource
  budget. `CUDA_VISIBLE_DEVICES` is empty for these jobs.
- Higher `--priority` values run first, then submission order among jobs whose
  dependencies succeeded. Ready jobs do not backfill around a job waiting for
  resources, so a waiting two-GPU job cannot be starved by newer one-GPU jobs.
  This can temporarily leave a GPU idle. Jobs waiting on dependencies do not
  block unrelated ready work.
- The shared budget defaults to the machine's cores minus four (at most 16) and
  its RAM minus 6 GiB (at most 24 GiB), leaving room for the OS and desktop. Set
  it with `install.py --max-cpus N --max-memory 24g` or in `config.json`. Each job also has an enforced CPU/RAM limit. Defaults are
  4 CPUs and 8 GiB per requested GPU, or 4 CPUs/8 GiB for a CPU-only job.
  Request the RAM your experiment actually needs; shared memory is included in
  the container's RAM limit. Default Docker shared memory is 2 GiB, configurable
  with `--shm-size`.
- The queue waits on unmanaged CUDA processes, GPU-enabled containers (including
  sleeping containers), excess GPU memory or activity, and insufficient host
  available RAM. Known desktop processes have a small measured idle allowance.
  If GPU/Docker/systemd observations fail, dispatch stops until checks recover.
- Reservations coordinate submitted jobs. Route research launches through this
  queue: the queue cannot prevent an independent direct launch from taking a
  GPU after an observation.

## Dependencies and experiment sweeps

```bash
prepare=$(gpuq submit --project my-project --gpus 0 --memory 4g \
  -- python preprocess.py)

train=$(gpuq submit --project my-project --image my-project:version \
  --after "$prepare" --gpus 1 --memory 12g -- python train.py)

gpuq submit --project my-project --image my-project:version \
  --after "$train" --gpus 1 -- python evaluate.py
```

Use `--after ID1,ID2` or repeated `--after` flags for multiple dependencies. If a
dependency fails or is cancelled/skipped, the dependent job becomes `skipped`.
Dependencies establish ordering; explicitly mount or pass their output paths
when later stages consume those artifacts. For independent seeds:

```bash
for seed in 1 2 3 4 5; do
  gpuq submit --project my-project --name "seed-$seed" \
    --image my-project:version --gpus 1 --memory 8g \
    -- python train.py --seed "$seed" --output /workspace/run/outputs
done
```

## Manage jobs

```bash
gpuq status                     # Pending jobs, physical GPUs, worker health
gpuq status --all                # Include retained history
gpuq status --project my-project
gpuq status --json               # Machine-readable status; no host socket needed
gpuq show 12                     # Command, resource request, mapping and run path
gpuq logs 12 -f                  # Saved log, followed until the job finishes
gpuq wait 12 13 --timeout 2h     # Exit 0 only if all requested jobs succeed
gpuq cancel 12 13                # Stop these jobs and their supervised children
gpuq retry 12                    # New ID and output directory; old attempt stays
gpuq retry 13 --after 14         # Retry with corrected dependency IDs
gpuq pause                      # Pause new starts; running work continues
gpuq resume
```

Add `--timeout 6h` to a submission to stop a job at its time limit. A job time
limit records failure with exit code 124. A `gpuq wait` time limit only stops the
wait; it does not cancel jobs. Failed jobs are never automatically replayed.
Retries reuse a recorded immutable Docker image and any existing source snapshot.

Pass selected environment variables with `--env NAME=value` or `--env NAME`
(capture its current value). `--env-file /absolute/path` loads `NAME=VALUE` lines
at execution. Environment values are private in the queue database and are
redacted in manifests and CLI JSON. Device assignments and `GPUQ_*` variables
are controlled by the queue. Large caches are shared per project; each run's
outputs remain separate.

## Labels and agent origin

`--label KEY=VALUE` (repeatable, up to 32) keeps small metadata with a job, such
as `--label seed=3 --label experiment=ablation`. When a job is submitted from
inside an agent run, gpuq also records which run asked for it: `codex-task` sets
`CODEX_TASK_RUN` and `CODEX_TASK_TURN` for Codex, and Claude Code sets
`CLAUDE_CODE_SESSION_ID`. Both appear under `spec.labels` and `spec.origin` in
`gpuq show`, and the lab notebook (`labbook/`) uses them to link an agent's
conversation to the jobs, logs and receipts it produced.

## Run records

State is private to this account in `~/.local/state/gpuq/` (or `$GPUQ_STATE_DIR`):

```text
config.json                    Physical UUIDs, budgets and detection thresholds
queue.sqlite3                  Durable jobs, dependencies and reservations
runs/000012/manifest.json       Submission, image ID, mapping, times, result
runs/000012/console.log         Combined stdout/stderr
runs/000012/outputs/            Checkpoints and results
runs/000012/tmp/                Temporary files
snapshots/source-.../           Optional source snapshot and file hashes
cache/<project>/               Persistent Docker cache
```

Jobs receive `GPUQ_JOB_ID`, `GPUQ_PROJECT`, `GPUQ_RUN_DIR`, `GPUQ_OUTPUT_DIR`,
`GPUQ_GPU_UUIDS`, and `GPUQ_GPU_INDICES`. The index variable describes physical
host indices; CUDA indices are logical. OMP/MKL/OpenBLAS/NumExpr thread counts
default to the job's CPU request and can be overridden deliberately.

## Service and recovery

```bash
systemctl --user status gpuq.service
journalctl --user -u gpuq.service -n 50 --no-pager
systemctl --user restart gpuq.service
gpuq doctor
```

The service is enabled and `Linger=yes`, so it starts at boot and persists after
logout. Restarting or stopping the worker leaves existing job supervisors
running. A worker restart adopts them using deterministic names and ownership
labels, keeping their reservations. A machine reboot interrupts experiments;
missing runtimes are recorded as failures and require explicit retries. Queued
jobs remain durable. There is one worker lock per queue and another per account
to prevent competing queue workers from allocating the same hardware.

Docker logs are copied incrementally to each run. Logs are retained across
worker restarts; a crash exactly between log append and cursor commit can repeat
a few lines. Docker keeps up to 300 MiB locally while the worker is unavailable.
Completed containers and transient units are removed after their logs/results
have been saved; run artifacts and failed attempts remain.

Edit `config.json` and restart the worker to change budgets. `gpuq pause` is the
way to stop new dispatches while continuing to observe and manage running jobs.
`gpuq status`, `show` and `logs` can be read from a restricted sandbox using cached
host observations. Host verification and workload execution still use the normal
approved host path when required by the calling environment.

The service and database are for this user on this machine. There is no remote
submission API or multi-user authorization layer.

## Installation and verification

```bash
python3 gpuq/install.py --max-cpus 16 --max-memory 24g
cd gpuq
python3 -m unittest discover -s tests -v
python3 tests/host_smoke.py --image YOUR_TORCH_IMAGE:TAG
```

The host smoke suite requires an idle queue and a known working Torch image. It
checks actual CUDA operations and device isolation, parallel one-GPU scheduling,
two-GPU exclusivity, CPU/RAM limits, dependencies, cancellation, time limits,
retry records, restart/SIGKILL recovery, unmanaged GPU containers, and service
persistence configuration. Verification reports are saved under `verification/`.
The installer preserves existing configuration and modifies only this user's
queue executable/service; it reuses the existing driver and Docker setup.

Resource enforcement follows [Docker's CPU/RAM controls](https://docs.docker.com/engine/containers/resource_constraints/)
and [NVIDIA's GPU device selection](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/docker-specialized.html).
