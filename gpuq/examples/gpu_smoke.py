"""Small real CUDA check used by tests/host_smoke.py."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

import torch

parser = argparse.ArgumentParser()
parser.add_argument("--devices", type=int, required=True)
parser.add_argument("--hold", type=float, default=8)
args = parser.parse_args()
assert torch.cuda.is_available()
assert torch.cuda.device_count() == args.devices, (torch.cuda.device_count(), args.devices)
visible = subprocess.run(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
                         capture_output=True, text=True, check=True).stdout.split()
assigned = os.environ["GPUQ_GPU_UUIDS"].split(",")
assert set(visible) == set(assigned), (visible, assigned)
for index in range(args.devices):
    values = torch.ones((64, 64), device=f"cuda:{index}")
    product = values @ values
    torch.cuda.synchronize(index)
    assert product[0, 0].item() == 64
result = {"job_id": int(os.environ["GPUQ_JOB_ID"]), "uuids": assigned,
          "logical_devices": args.devices, "torch": torch.__version__, "cuda": torch.version.cuda,
          "compute_start": time.time(), "pid": os.getpid()}
print(json.dumps(result), flush=True)
output = Path(os.environ["GPUQ_OUTPUT_DIR"]) / "smoke.json"
output.write_text(json.dumps(result, indent=2) + "\n")
time.sleep(args.hold)
result["compute_end"] = time.time()
output.write_text(json.dumps(result, indent=2) + "\n")
print("CUDA smoke passed", flush=True)
