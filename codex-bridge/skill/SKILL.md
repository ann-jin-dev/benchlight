---
name: codex-bridge
description: How to command and supervise the local Codex agent (OpenAI Codex CLI / Codex app) from Claude Code through the `codex-task` wrapper — dispatching a brief, waiting for Codex to finish or ask a question, reading and verifying its report, replying, steering or stopping a running turn, and recording verdicts. Use this whenever the user asks you to have Codex do something, to continue or check on a "[Claude] ..." thread or a run in ~/codex-runs, or when a turn starts with "[codex-task] Codex run ... ended". Also use it before giving the user a paste-ready Codex prompt, since you can usually send it yourself.
---

# Driving Codex with `codex-task`

The working model: **Claude sets direction and writes briefs; Codex executes them;
Claude verifies the results and reports to the user, who makes the decisions.**
`codex-task` (installed at `~/.local/bin/codex-task`) is the bridge. Each run is a normal
interactive Codex thread named `[Claude] <name>`, so the user can watch it in the Codex
app. Codex wakes you when a turn ends, so you don't poll.

Details: `codex-task --help` and the docstring at the top of the script. Run state lives
in `~/codex-runs/<run-id>/`: `meta.json`, `notes.jsonl`, and per turn
`turns/<n>/prompt.md`, `events.jsonl`, `final.md`, `ended`.

## Every Codex thread must be visible to the user

Only `codex-task start` and `resume` guarantee this. They drive `codex app-server`, the
protocol the Codex app itself uses, so the thread is stored like an app thread.

- Never run `codex exec`, `codex` or `codex app-server` yourself, not even for a quick
  task. `codex exec` threads are hidden from the app.
- After the first turn of a new run starts, check visibility with
  `python3 ~/.claude/skills/codex-bridge/scripts/list_threads.py <project dir>`. This
  read-only probe asks a fresh app-server for `thread/list`, the query the app uses.
- A running Codex app does not refresh its sidebar for threads created by another
  process. If the probe lists the thread but the user can't see it, they should reload
  the app.
- When you start a run, tell the user the thread name and project, and remind them not
  to type into it while a turn is running.

## Permissions: run it as a bare command

Allow `Bash(codex-task *)` in `~/.claude/settings.json`, and run every `codex-task` call
as its own Bash command: no `cd … &&`, pipes, `;`, or env-var prefixes. Compound commands
fall outside the allow rule and may be blocked by the permission classifier.

## The loop

### 1. Write a brief

Put the brief in the project next to its other records, e.g.
`<project>/records/<YYYY-MM-DD>-<slug>/BRIEF.md`. Codex only knows what is in the brief
and the repository, so make it self-contained:

- **Question**: what decision the result feeds, in 2–4 lines.
- **Tasks**: numbered and concrete, with the entry points and configs to reuse.
- **Decision rule fixed in advance**: what result means go or no-go, so the outcome
  can't be rationalized afterwards.
- **Constraints**: time box, compute budget, GPU rules, data that must not be touched,
  and what *not* to start yet.
- **Deliverables**: the report path, the scripts, and what goes under "Next".

`start` appends an operating protocol automatically: work autonomously, route GPU work
through `gpuq`, and end each turn with `## Summary / ## Results / ## Decisions made /
## Artifacts / ## Next` and a last line `STATUS: DONE | QUESTION | BLOCKED`.

### 2. Start the run

```
codex-task start --name <short-kebab-name> --cd <project dir> --brief <BRIEF.md>
```

Model and reasoning effort default to `CODEX_TASK_MODEL` / `CODEX_TASK_EFFORT`, or to
Codex's own configuration when those are unset. Command approvals use Codex's
auto-review in a `workspace-write` sandbox. Your Claude session id is recorded for
unattended wakes. Short instructions can be given inline instead of a file.

### 3. Wait in the background

```
codex-task wait <run-id> --timeout <seconds>
```

Run it in the background. It costs nothing while blocked, and you are re-invoked when
it exits:

- **`TURN ENDED run … STATUS: X`** plus Codex's final report: go to step 4.
- **`CHECK-IN (timer Ns)`** plus a compact snapshot (recent commands, messages, gpuq
  jobs), exit code 3: glance at it, `steer` if Codex is going off track, otherwise wait
  again.

Use about 300–600 s early in a new brief (to catch a misunderstanding) and 1800–3600 s
for multi-hour work. Keep a waiter running whenever a turn is in flight.

### 4. Read, verify, record, report

Read the report critically before relaying it. Open the files it points to, check the
key numbers against the source tables, and run a small CPU check if a claim carries
weight. Then record your verdict on the run's timeline:

```
codex-task note --by claude --kind verdict <run-id> 'Checked results.csv: 5 seeds, gain holds (CI excludes 0)'
```

When the user decides something about the run, record it the same way with
`--by human --kind decision`. Options go before the run id. Then tell the user the
conclusion first, briefly.

- `STATUS: DONE`: verify, summarize, propose the next step.
- `STATUS: QUESTION`: answer it yourself if it is within what the user already decided
  or is a small diagnostic; otherwise bring it to the user with Codex's recommended
  default.
- `STATUS: BLOCKED`: diagnose (`codex-task show <id>`, the turn's `stderr.log` and
  `events.jsonl`), then fix the brief or ask the user.

### 5. Continue the same thread

```
codex-task resume <run-id> 'next instruction'
```

Then wait again. Keep one line of work in one run so Codex keeps its context. `resume`
does not re-append the protocol, so for a new phase point to its brief file and ask for
the usual final report. Use single quotes so the shell doesn't expand `$` or backticks.

## Other commands

| Command | Use |
|---|---|
| `codex-task status [id]` | list runs: turn, state, last STATUS, wake count |
| `codex-task show <id> [--turn N]` | final report, or a progress snapshot if running |
| `codex-task steer <id> 'msg'` | add guidance to the running turn |
| `codex-task stop <id>` | interrupt the running turn; then `resume` with new instructions |
| `codex-task note [--by B] [--kind K] <id> 'text'` | record a verdict, decision or note |
| `codex-task adopt <id>` | fork an old `codex exec` run into an app-visible thread |
| `codex-task halt` / `unhalt` | turn unattended wakes off / on (kill switch) |

## Unattended wakes and their limits

If a turn ends while no `wait` is running and your Claude process has exited, the hook
runs `claude -p --resume <your session>` in `dontAsk` mode with a narrow allowlist: bare
`codex-task …`, `gpuq status…`, Read, Grep and Glob. Each run allows at most
`--max-wakes` (default 20) wakes, and `codex-task halt` disables them all. The woken turn
begins with `[codex-task] Codex run <id> turn <n> ended (unattended wake k/N)`.

In a wake turn, read the result with `show` and continue only with steps the user has
already approved. If the run needs a user decision, stop and write a summary. Wakes go
to the session recorded at `start`; for a run inherited from another session, keep a
`wait` alive instead. Every wake is logged in `~/codex-runs/<id>/wake.log`.

## Provenance

Each turn's Codex process carries `CODEX_TASK_RUN` and `CODEX_TASK_TURN`. When Codex
submits a job with `gpuq submit`, the job records that origin, so the lab notebook can
replay a run as one timeline: brief → Codex turns → GPU jobs and their receipts →
verdicts and decisions from `notes.jsonl`.

## Rules worth keeping

- Ask the user before big compute or new data generation (multi-hour jobs, training,
  scale-ups). Small diagnostics can be dispatched directly.
- GPU work goes through `gpuq`; it can't run inside the Codex sandbox.
- Compare against the baseline the user chose for the method's purpose. Harder
  controls, floors and oracles are diagnostics, not the bar.
- Keep comparisons fair: matched inputs and budget, per-method calibration, several
  seeds, and no selection on data already used for selection.
- Keep reports to the user short and conclusion-first.

## Gotchas

- The wrapper's `--sandbox` flag replaces auto-review with a fixed sandbox and no
  approvals; use it only if asked.
- An approval request that reaches the wrapper is declined. Repeated declines in
  `events.jsonl` usually mean a command needs the host, e.g. GPU work belongs in gpuq.
- `steer` needs a running turn; `resume` needs an ended one.
- Threads the user started in the Codex app have no run record, so `codex-task` can't
  drive them. Write a paste-ready prompt for the user instead, or start a new run whose
  brief points at that thread's reports.
