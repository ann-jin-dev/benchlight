# codex-bridge: Claude supervises, Codex executes

`bin/codex-task` lets Claude Code hand a written brief to the local Codex agent,
wait for the result without polling, verify it, and continue the same Codex
thread. `skill/` is the Claude Code skill that teaches Claude the loop.

```
Claude writes BRIEF.md ──▶ codex-task start ──▶ Codex works (gpuq for GPUs)
        ▲                                              │
        │                        turn ends: report + STATUS line
        └── verify, note verdict, ask the user ◀── codex-task wait
```

## What it does differently

- **Visible threads.** Runs go through `codex app-server`, the protocol the
  Codex app uses, so every run is an ordinary thread named `[Claude] <name>`
  that a person can watch in the Codex app.
- **No polling.** `codex-task wait <id> --timeout S` blocks until the turn ends
  or a check-in timer fires. Claude runs it in the background and is woken
  with either the full report or a compact progress snapshot.
- **A fixed report contract.** Every turn ends with Summary / Results /
  Decisions made / Artifacts / Next and `STATUS: DONE | QUESTION | BLOCKED`,
  which the supervisor can act on.
- **Bounded unattended wakes.** If a turn ends after the Claude session has
  exited, the Codex notify hook resumes that session headlessly with a narrow
  tool allowlist, at most `--max-wakes` times per run. `codex-task halt` turns
  this off everywhere.
- **Provenance.** Each turn's Codex process carries `CODEX_TASK_RUN` and
  `CODEX_TASK_TURN`, which `gpuq submit` stores with every job. `codex-task
  note` records verification verdicts and human decisions. The lab notebook
  joins these into one replayable timeline.

## Install

`install.sh` links `bin/codex-task` into `~/.local/bin` and copies `skill/` to
`~/.claude/skills/codex-bridge/`. Allow the command in
`~/.claude/settings.json`:

```json
{"permissions": {"allow": ["Bash(codex-task *)", "Read(~/codex-runs/**)"]}}
```

Optional: `CODEX_TASK_MODEL` and `CODEX_TASK_EFFORT` choose the model and
reasoning effort; otherwise Codex's own configuration applies.
`CODEX_RUNS_DIR` moves run records from `~/codex-runs`.

## Commands

| Command | Use |
|---|---|
| `codex-task start --name N --cd DIR --brief FILE` | start a run (turn 1) |
| `codex-task wait <id> --timeout S` | block until the turn ends or the timer fires |
| `codex-task show <id> [--turn N]` | final report, or a progress snapshot |
| `codex-task resume <id> 'message'` | next turn in the same Codex thread |
| `codex-task steer <id> 'message'` | add guidance to the running turn |
| `codex-task stop <id>` | interrupt the running turn |
| `codex-task note [--by B] [--kind K] <id> 'text'` | record a verdict, decision or note |
| `codex-task status [id]` | list runs |
| `codex-task halt` / `unhalt` | disable / enable unattended wakes |

## Run records

```text
~/codex-runs/<run-id>/
  meta.json                  name, project, model, Codex thread, wake budget
  notes.jsonl                verdicts and decisions (codex-task note)
  wake.log                   every unattended wake and why it did or didn't happen
  turns/<n>/prompt.md        what Claude sent
  turns/<n>/events.jsonl     Codex's commands, edits and messages
  turns/<n>/final.md         Codex's final report
  turns/<n>/turn_id          the Codex turn id (joins token usage in ~/.codex/sessions)
```
