# Remote control that heals itself

The workstation is meant to keep working while nobody sits at it. Three
pieces keep it reachable and recover from crashes, reboots and network
outages without a person logging in.

## Claude Code Remote Control

`claude-remote-control.service.in` runs `claude remote-control` as a systemd
user service in a chosen working directory. The session appears in
claude.ai/code and the Claude mobile app, so Claude can be given work on this
machine from anywhere. `install.sh --with-remote-control DIR` fills in the
template and enables it.

Why it recovers by itself:

- `Restart=always` with `RestartSec=10s` restarts it after any exit, including
  a clean one after a network drop.
- `StartLimitIntervalSec=0` disables systemd's start-rate limit, so a long
  outage never leaves the unit in a failed state that needs a manual reset.
- `loginctl enable-linger` starts user services at boot, before anyone logs in.

Check it with `systemctl --user status claude-remote-control` and
`journalctl --user -u claude-remote-control -n 50`.

## Codex app-server daemon

Codex ships its own durable daemon for remote use. The Codex CLI manages it:

```bash
codex app-server daemon bootstrap               # durable local management
codex app-server daemon enable-remote-control   # for future and running daemons
codex app-server daemon version                 # CLI and running daemon versions
```

The daemon keeps a PID-update loop and an updater, so it survives app restarts.
`codex-task` does not depend on it: each turn starts its own short-lived
`codex app-server` over stdio.

## Unattended agent wakes

When Codex finishes a turn and the supervising Claude session has exited,
`codex-task` resumes that session headlessly (`claude -p --resume`) so the work
continues. That path is bounded on purpose: a narrow tool allowlist, a
per-run wake budget, and a `codex-task halt` kill switch. See
[`../codex-bridge`](../codex-bridge) and [`../docs/safety.md`](../docs/safety.md).

## Headless remote desktop

GNOME Remote Desktop needs a monitor. `headless-display/virtual-display.sh`
forces a disconnected integrated-GPU DisplayPort connector on through DRM
debugfs and adds a 1080p mode, so the desktop can be shared with no screen
attached. It needs passwordless sudo for its two debugfs commands and expects
an AMD iGPU by default; the variables at the top of the script select another
connector or vendor. `virtual-display.desktop.in` runs it at login through
GNOME autostart.
