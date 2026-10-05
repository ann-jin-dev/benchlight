#!/usr/bin/env bash
# Run install.sh against a throwaway HOME with stubbed host commands, then check
# what it installed, that a second run is idempotent, and that it refuses to
# replace a file it did not create.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sandbox="$(mktemp -d)"
trap 'rm -rf "$sandbox"' EXIT
stubs="$sandbox/bin"
home="$sandbox/my home"   # a space must survive into the unit files
mkdir -p "$stubs" "$home"

stub() { printf '#!/usr/bin/env bash\n%s\n' "$2" > "$stubs/$1"; chmod +x "$stubs/$1"; }
stub docker 'echo 27.0.0'
stub nvidia-smi 'echo "0, GPU-test-0000, Test GPU, 16303, 400, 0, 580.00"'
stub systemctl 'case "$*" in *show*) echo Version=255 ;; esac'
stub loginctl 'echo Linger=no'

run_install() {
  env -i HOME="$home" PATH="$stubs:/usr/bin:/bin" USER=tester \
    bash "$repo/install.sh" --no-start --max-cpus 2 --max-memory 2g \
    --profile "$repo/pulse/profiles/x870-dual-rtx5080.json"
}

check() { [[ -e "$1" || -L "$1" ]] || { echo "missing: $1" >&2; exit 1; }; }

run_install > "$sandbox/first.log"
h="$home"
for path in .local/bin/gpuq .local/bin/codex-task .local/bin/labbook \
            .config/systemd/user/gpuq.service .config/systemd/user/system-pulse-agent.service \
            .config/systemd/user/labbook.service .config/system-pulse/profile.json \
            .claude/skills/codex-bridge/SKILL.md .local/state/gpuq/config.json; do
  check "$h/$path"
done
grep -q '"max_cpus": 2' "$h/.local/state/gpuq/config.json"
grep -qF "ExecStart=/usr/bin/python3 \"$repo/labbook/labbook\" serve --port 8787" "$h/.config/systemd/user/labbook.service"
grep -qF "Environment=\"PATH=$h/.local/bin:" "$h/.config/systemd/user/system-pulse-agent.service"
if grep -q '@[A-Z_]*@' "$h"/.config/systemd/user/*.service; then echo "unrendered placeholder" >&2; exit 1; fi

run_install > "$sandbox/second.log"   # idempotent

echo "[Unit]" > "$h/.config/systemd/user/labbook.service"
if run_install > "$sandbox/third.log" 2>&1; then
  echo "install.sh replaced a file it did not create" >&2; exit 1
fi
grep -q "refusing to replace" "$sandbox/third.log"

quoted="$sandbox/bad\"home"   # a quote would break the unit files' quoting
mkdir -p "$quoted"
if env -i HOME="$quoted" PATH="$stubs:/usr/bin:/bin" USER=tester bash "$repo/install.sh" --no-start \
    > "$sandbox/fourth.log" 2>&1; then
  echo "install.sh accepted a HOME containing a quote" >&2; exit 1
fi
grep -q "may not contain quotes" "$sandbox/fourth.log"
echo "install test passed"
