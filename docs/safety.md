# Safety model

Coding agents on this machine can run commands, write files and start multi-hour GPU
jobs. The design assumes they will sometimes be wrong, and makes the costly mistakes
either impossible or visible.

## What could go wrong, and what prevents it

| Risk | Guardrail |
|---|---|
| Two agents (or an agent and a person) start work on the same GPU and both crash or silently slow down | GPUs are reserved exclusively, by physical UUID, inside one SQLite transaction. gpuq refuses to schedule onto a card that has an unmanaged CUDA process, a GPU-enabled container it does not own, unexpected memory use or utilization. |
| A host check fails and the scheduler guesses | Any uncertain observation (nvidia-smi, Docker or systemd failing) stops new dispatches and keeps existing reservations until checks recover. |
| A crashed job is re-run automatically and doubles a result or a cost | gpuq never replays a job. A retry is an explicit command with a new job ID; the failed attempt and its logs stay. |
| A scheduler restart orphans or kills running experiments | Jobs run as Docker containers or transient systemd units outside the worker, with deterministic names and ownership labels. A restarted worker adopts them and checks ownership before stopping anything. |
| Secrets end up in reports | Environment values are stored privately in the queue and replaced by `<set>` in manifests, CLI JSON and receipts. |
| An agent's work happens where no person can see it | `codex-task` runs every Codex turn through the app-server protocol, so it appears as a normal named thread in the Codex app. The skill forbids `codex exec`. |
| Codex asks for an approval nobody is there to give | Approval requests reaching the wrapper are declined, not auto-accepted. Codex's own auto-review handles routine approvals inside a workspace-write sandbox. |
| An unattended agent keeps going and spends money or compute | A headless wake-up happens only when the supervising Claude session has exited and no waiter is running. It runs in `dontAsk` mode with a narrow allowlist (`codex-task`, `gpuq status`, Read, Grep, Glob), at most `--max-wakes` times per run, and `codex-task halt` disables all of them. Every wake is logged. The skill tells Claude to continue only with steps the user has already approved. |
| Agents submit GPU work outside the queue | Agent instructions (`AGENTS.md`, the codex-task protocol) route all GPU work through gpuq, and gpuq blocks scheduling while unmanaged GPU processes exist. This is a convention plus detection, not a hard sandbox: a process started outside the queue can still use a free card. |
| The dashboard leaks private data | The notebook listens only on 127.0.0.1 and checks the `Host` header. The public window is a separate process that serves an allowlisted summary: no hostnames, paths, commands, job names, logs or transcripts; projects only under chosen aliases. |
| Telemetry needs root | System Pulse needs no passwordless sudo. Reading CPU package energy is granted to a group by a udev rule, with the PLATYPUS side-channel trade-off documented next to it. |

## What is deliberately out of scope

- Multiple users or untrusted local accounts. Everything runs under one account, and
  anyone who can act as that account can do anything the agents can.
- Containing a malicious agent. The guardrails stop accidents and make actions visible;
  they are not a security boundary against code that tries to escape them.
- Remote job submission. Nothing listens for commands on the network. Remote access goes
  through Claude Code Remote Control and Codex's own daemon, which authenticate with the
  owner's accounts.

## Kill switches

```bash
gpuq pause                 # stop new GPU jobs; running ones continue
gpuq cancel <job> ...      # stop specific jobs and only their runtimes
codex-task halt            # no more unattended Claude wake-ups
codex-task stop <run>      # interrupt a running Codex turn
systemctl --user stop claude-remote-control
```
