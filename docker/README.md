# AI research Docker environments

Each experiment gets its own directory, Docker image, Python dependency list, code mount, data mount, and output mount. This needs Docker Engine, Docker Compose, and the NVIDIA Container Toolkit; on Ubuntu, `setup-host.sh` installs them.

## Create an experiment

```bash
cd benchlight/docker
bash new-experiment.sh my-study
cd experiments/my-study
```

Edit `requirements.txt` to pin packages for this experiment. Edit `Dockerfile` to change CUDA/cuDNN or install system packages. Changes in one experiment do not alter another experiment's image. Run `docker compose build gpu0` after changing these files.

The generated `.env` uses the current Linux UID/GID so bind-mounted files stay owned by you. Change `DATA_DIR` to an absolute host path if several experiments use the same dataset. Data is mounted read-only; write checkpoints and results to `outputs`.

## Queue experiments across projects

The shared `gpuq` service reserves the workstation's GPUs across all projects.
New experiments receive `RESEARCH_IMAGE=research-experiments:NAME` in `.env`.
Build the environment, then submit the training command from the experiment
directory (replace `train.py` and its arguments with your actual program):

```bash
docker compose build gpu0
gpuq submit --project my-study --image research-experiments:my-study \
  --cwd "$PWD/code" --gpus 1 --cpus 4 --memory 8g \
  --mount "$PWD/data:/workspace/data:ro" --cache-dir "$PWD/cache" \
  --env DASHBOARD_PROGRESS_PATH=/workspace/run/outputs/progress.json \
  -- python train.py --output /workspace/run/outputs
gpuq status
gpuq logs JOB_ID -f
```

Use `--gpus 2` for a command that needs both GPUs. Queued jobs get separate logs
and output directories under `~/.local/state/gpuq/runs/`. For native commands,
dependencies, cancellation, retries, and source snapshots, see the
[GPU queue guide](../gpuq/README.md). Older experiment directories keep their
existing Compose image names; pass the built image's tag or ID to `--image`.

## Direct GPU services

Use `gpuq` for scheduled research work. Direct Compose services below expose
devices without taking queue reservations; a running GPU service, including a
sleeping one, makes that GPU unavailable to queued jobs.

From the experiment directory, choose one service:

```bash
docker compose up -d --build gpu0  # physical GPU 0
docker compose up -d --build gpu1  # physical GPU 1
docker compose up -d --build both  # both GPUs
```

Check the selected cards by UUID:

```bash
docker compose exec gpu0 nvidia-smi --query-gpu=uuid,name --format=csv,noheader
docker compose exec gpu0 bash
docker compose --profile gpu0 down
```

For a one-off command, use `docker compose run --rm gpu0 python train.py`. Substitute `gpu1` or `both` as needed. Different experiment directories have different Compose project names, so they can run simultaneously. The GPU assignment controls which devices a container can see; it does not prevent two containers from sharing the same GPU.

When using `gpu1` or `both`, use the matching profile in the stop command: `docker compose --profile gpu1 down` or `docker compose --profile both down`.

Direct Docker examples (outside Compose):

```bash
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
docker run --rm --gpus '"device=0"' nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
docker run --rm --gpus '"device=1"' nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
docker run --rm --gpus '"device=0,1"' nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
```

Docker access without `sudo` begins after you sign out of the desktop session and sign back in. Membership in the `docker` group grants root-level host access. Until then, prefix Docker commands with `sudo`.

## Reinstall or update the host tooling

`bash docker/setup-host.sh` configures Docker's and NVIDIA's signed APT repositories and installs the required packages. It does not install or change the NVIDIA GPU driver.

## Report progress to System Pulse

Experiment code/progress.py writes a small progress file to the mounted outputs
directory. Add this call to a training loop:

    from progress import report_progress

    report_progress(step=120, total_steps=1000, metric_name="loss", metric_value=0.38)

Call it again with status="completed" at the end. System Pulse reads the file
and displays steps, percentage, metric, and last update alongside Docker state.
