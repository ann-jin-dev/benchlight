# Architecture

The stack is five small programs that share one rule: **each component owns its
records, and the others only read them.** There is no message bus, database server or
network API between components. Every record is a file under the user's home directory,
so each piece can be stopped, upgraded or replaced alone, and the whole system can be
inspected with `cat` and `sqlite3`.

## Who writes what

| Writer | Records | Readers |
|---|---|---|
| `gpuq` worker and CLI | `~/.local/state/gpuq/queue.sqlite3` (jobs, reservations, dependencies), `runs/NNNNNN/manifest.json`, `snapshots/source-*/.gpuq-source.json` | System Pulse (`gpuq status --json`), labbook (SQLite, read-only) |
| System Pulse agent | `~/.local/state/system-pulse/latest.json`, `history.sqlite3`, `energy.sqlite3` | labbook |
| `codex-task` | `~/codex-runs/<run>/meta.json`, `notes.jsonl`, `wake.log`, `turns/<n>/{prompt.md,events.jsonl,final.md,turn_id}` | labbook, Claude |
| Codex itself | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (token usage per turn) | labbook |
| labbook | nothing | the browser |

labbook opens SQLite files with `mode=ro` and `PRAGMA query_only`, so it can never
change the queue or the meter, and it still sees the live write-ahead log.

## How a receipt is assembled

```
gpuq job ──► spec (command, image tag, cwd, git, labels, origin)
          ├► runtime (image ID actually started, physical GPUs, queue instance)
          └► source snapshot manifest ──► tree hash over (path, sha256)
energy meter ──► (queue instance, job id) ──► GPU mJ, CPU mJ, metered seconds, per-minute power
agent link ──► origin.codex_run / turn          (exact: recorded at submission)
            ├► job id printed by `gpuq submit`  (exact: found in the turn's command log)
            └► time window + project folder     (inferred)
            ──► run meta, turn report, Codex token usage, notes (verdicts, decisions)
canonical JSON of job, times, code, environment, hardware, energy
            ──► SHA-256 digest (once the job is final; `labbook verify` re-checks a copy)
```

The queue instance ID is part of every key, so job numbers from a reinstalled queue never
collide with old energy records.

## How energy is attributed

System Pulse samples every five seconds. For each interval it reads:

- each GPU's NVML total-energy counter (millijoules since the driver loaded),
- the CPU package RAPL counter (microjoules, wraps around),
- the machine's busy CPU time from `/proc/stat`,
- each running gpuq job's `cpu.stat` from its own cgroup (a Docker scope or a transient
  systemd unit).

A job's GPU energy for the interval is the sum of the counter differences of the cards
reserved to it. Because gpuq reservations are exclusive, no apportioning is needed: the
card's whole draw, idle draw included, belongs to the job that holds it. CPU energy is the
package energy times the job's share of busy CPU time. Intervals longer than 30 s, across a
reboot, or with a counter that went backwards are skipped and recorded as unmetered.

## How a run is replayed

`labbook` turns one `codex-task` run into a sorted event list: each turn's prompt (with the
appended protocol stripped), steering messages, the turn's end with its report summary and
token count, every gpuq job linked to the run (queued and finished), unattended wake-ups
from `wake.log`, and notes from `notes.jsonl`. The browser groups bursts of job events and
lets the reader step through the list.

## The two servers

- `labbook serve` binds 127.0.0.1 and refuses requests whose `Host` header is not a
  loopback name, which blocks DNS-rebinding pages from reading it.
- `labbook public` is a separate process with its own handler. It serves one page and one
  JSON document built field by field from an allowlist (see `labbook/lab/public.py`). It
  caches that document for five seconds so many visitors cost one build.

Both use only `http.server` from the standard library and a strict Content-Security-Policy
(`default-src 'self'`); every string from a record enters the page through `textContent`.
