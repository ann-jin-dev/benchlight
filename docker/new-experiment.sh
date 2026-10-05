#!/usr/bin/env bash
set -euo pipefail

name="${1:-}"
if [[ ! "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ || ${#name} -gt 128 ]]; then
  echo "Usage: $0 EXPERIMENT_NAME (1-128 letters, digits, underscores or hyphens)" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
destination="$root/experiments/$name"
if [[ -e "$destination" ]]; then
  echo "Experiment already exists: $destination" >&2
  exit 1
fi

mkdir -p "$root/experiments"
cp -a "$root/experiment-template" "$destination"
mkdir -p "$destination/code" "$destination/data" "$destination/outputs" "$destination/cache"
cat > "$destination/.env" <<EOF
HOST_UID=$(id -u)
HOST_GID=$(id -g)
RESEARCH_IMAGE=research-experiments:${name}
CODE_DIR=./code
DATA_DIR=./data
OUTPUT_DIR=./outputs
CACHE_DIR=./cache
EOF

echo "$destination"
