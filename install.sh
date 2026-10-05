#!/usr/bin/env bash
# Set up the workstation stack for the current user on one Ubuntu + NVIDIA machine.
#
#   ./install.sh                         gpuq, System Pulse, labbook, codex-bridge
#   ./install.sh --with-remote-control ~/projects
#
# Everything runs as systemd *user* services. Nothing here touches drivers,
# Docker's configuration or root services; docker/setup-host.sh does the
# one-time host setup. Files this script did not create are never replaced.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
marker="# Installed by install.sh"
max_cpus="" max_memory="" profile="" remote_dir="" remote_name="AI Workstation"
start=1 check_only=0 port=8787

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  cat <<EOF
Options:
  --max-cpus N               CPU budget shared by queued jobs
  --max-memory SIZE          RAM budget shared by queued jobs, e.g. 24g
  --profile FILE             System Pulse hardware profile (see pulse/profiles/)
  --port N                   labbook port on 127.0.0.1 (default 8787)
  --with-remote-control DIR  keep a Claude Code Remote Control session open in DIR
  --remote-name NAME         session name shown in Claude (default "$remote_name")
  --no-start                 install files without enabling or starting services
  --check                    only check prerequisites
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-cpus) max_cpus="$2"; shift 2 ;;
    --max-memory) max_memory="$2"; shift 2 ;;
    --profile) profile="$2"; shift 2 ;;
    --port) port="$2"; shift 2 ;;
    --with-remote-control) remote_dir="$2"; shift 2 ;;
    --remote-name) remote_name="$2"; shift 2 ;;
    --no-start) start=0; shift ;;
    --check) check_only=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# --- prerequisites -----------------------------------------------------------
missing=0
need() {  # need COMMAND HINT
  if command -v "$1" >/dev/null 2>&1; then
    printf '  ok       %s\n' "$1"
  else
    printf '  missing  %s  (%s)\n' "$1" "$2"; missing=1
  fi
}
say "Checking prerequisites"
need python3 "Python 3.10 or newer"
need systemctl "systemd"
need docker "run docker/setup-host.sh"
need nvidia-smi "install the NVIDIA driver"
if command -v python3 >/dev/null && ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "  Python 3.10 or newer is required"; missing=1
fi
if command -v systemctl >/dev/null && ! systemctl --user show --property=Version >/dev/null 2>&1; then
  echo "  No systemd user session (log in on the machine, or enable linger)"; missing=1
fi
if command -v docker >/dev/null && ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
  echo "  Docker is installed but this user cannot reach it (docker group?)"; missing=1
fi
[[ $missing -eq 0 ]] || fail "fix the missing prerequisites above, then re-run"
[[ $check_only -eq 1 ]] && { say "All prerequisites are present"; exit 0; }

# systemd unit values are quoted; refuse characters that would break the quoting.
for value in "$repo" "$HOME" "$remote_dir" "$remote_name"; do
  case "$value" in
    *'"'*|*\\*|*$'\n'*) fail "paths and names may not contain quotes, backslashes or newlines: $value" ;;
  esac
done

units="$HOME/.config/systemd/user"
mkdir -p "$units" "$HOME/.local/bin"

# Write a file only if it is absent or was written by this script before.
install_managed() {  # install_managed DESTINATION (content on stdin)
  local destination="$1" content
  content="$(cat)"
  if [[ -e "$destination" ]] && ! head -n 3 "$destination" | grep -qF "$marker"; then
    fail "refusing to replace $destination (not installed by install.sh)"
  fi
  printf '%s\n' "$content" > "$destination"
}

render() {  # render TEMPLATE KEY=VALUE...
  local template="$1"; shift
  python3 - "$template" "$@" <<'EOF'
import sys
text = open(sys.argv[1]).read()
for pair in sys.argv[2:]:
    key, _, value = pair.partition("=")
    # Values sit inside double quotes in the units; escape systemd specifiers
    # (%) and variable expansion ($). Quotes and backslashes are refused earlier.
    text = text.replace("@" + key + "@", value.replace("%", "%%").replace("$", "$$"))
sys.stdout.write(text)
EOF
}

link_managed() {  # link_managed TARGET LINK
  local target="$1" link="$2"
  if [[ -L "$link" && "$(readlink -f "$link")" == "$(readlink -f "$target")" ]]; then
    return
  fi
  [[ -e "$link" || -L "$link" ]] && fail "refusing to replace $link (it points elsewhere)"
  ln -s "$target" "$link"
}

# --- gpuq ----------------------------------------------------------------------
say "Installing gpuq (GPU queue)"
gpuq_args=()
[[ -n "$max_cpus" ]] && gpuq_args+=(--max-cpus "$max_cpus")
[[ -n "$max_memory" ]] && gpuq_args+=(--max-memory "$max_memory")
[[ $start -eq 0 ]] && gpuq_args+=(--no-start)
(cd "$repo/gpuq" && python3 install.py "${gpuq_args[@]}")

# --- System Pulse --------------------------------------------------------------
say "Installing System Pulse (telemetry and energy meter)"
mkdir -p "$HOME/.config/system-pulse"
if [[ -n "$profile" ]]; then
  if [[ -e "$HOME/.config/system-pulse/profile.json" ]]; then
    echo "  keeping existing ~/.config/system-pulse/profile.json"
  else
    install -m 0644 "$profile" "$HOME/.config/system-pulse/profile.json"
  fi
fi
render "$repo/pulse/system/system-pulse-agent.service.in" PULSE_DIR="$repo/pulse" HOME="$HOME" \
  | install_managed "$units/system-pulse-agent.service"

# --- labbook -------------------------------------------------------------------
say "Installing labbook (receipts, replay, dashboard on 127.0.0.1:$port)"
render "$repo/labbook/system/labbook.service.in" LABBOOK_DIR="$repo/labbook" HOME="$HOME" PORT="$port" \
  | install_managed "$units/labbook.service"
link_managed "$repo/labbook/labbook" "$HOME/.local/bin/labbook"

# --- codex-bridge --------------------------------------------------------------
say "Installing codex-bridge (codex-task and the Claude Code skill)"
link_managed "$repo/codex-bridge/bin/codex-task" "$HOME/.local/bin/codex-task"
skill="$HOME/.claude/skills/codex-bridge"
if [[ -e "$skill" && ! -e "$skill/.installed-by-benchlight" ]]; then
  echo "  keeping existing $skill (not installed by install.sh)"
else
  mkdir -p "$skill"
  cp -R "$repo/codex-bridge/skill/." "$skill/"
  touch "$skill/.installed-by-benchlight"
fi

# --- optional remote control ---------------------------------------------------
if [[ -n "$remote_dir" ]]; then
  say "Installing Claude Code Remote Control for $remote_dir"
  [[ -d "$remote_dir" ]] || fail "$remote_dir is not a directory"
  render "$repo/remote-control/claude-remote-control.service.in" \
      WORKDIR="$(cd "$remote_dir" && pwd)" HOME="$HOME" NAME="$remote_name" \
    | install_managed "$units/claude-remote-control.service"
fi

# --- services ------------------------------------------------------------------
systemctl --user daemon-reload
if [[ $start -eq 1 ]]; then
  services=(system-pulse-agent.service labbook.service)
  [[ -n "$remote_dir" ]] && services+=(claude-remote-control.service)
  systemctl --user enable --now "${services[@]}"
fi

say "Done"
cat <<EOF
  gpuq status                       # the queue
  labbook open                      # receipts, run replay and live dashboard
  systemctl --user status gpuq system-pulse-agent labbook
EOF
if [[ "$(loginctl show-user "$(id -u)" -p Linger 2>/dev/null)" != "Linger=yes" ]]; then
  echo "  To keep services running after logout and start them at boot:"
  echo "    loginctl enable-linger $(id -un)"
fi
