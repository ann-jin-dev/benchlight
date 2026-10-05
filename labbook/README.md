# labbook: receipts, run replay and a public window

labbook reads the records the other components keep (gpuq's queue, System Pulse's
snapshot and energy meter, codex-task's run folders and Codex's token logs) and turns
them into three views. It never writes to any of them. Standard library only.

```bash
labbook demo                 # invented records, no GPU needed: :8787 and :8788
labbook serve                # private notebook on http://127.0.0.1:8787
labbook public               # public window on http://127.0.0.1:8788 (expose only this)
labbook receipt 1097         # one job's receipt in the terminal (--json for the record)
labbook verify receipt.json  # check a shared receipt against its digest
labbook runs                 # agent runs
labbook replay <run-id>      # a run's timeline in the terminal
labbook public-json          # exactly what the public window would publish
```

## Views

- **Live**: power draw (UPS-measured or estimated), GPU cards with the job holding each
  and its energy so far, the queue with scheduler notes, temperatures and the outlet
  estimate's breakdown.
- **Receipts**: every job, filterable by project, status and whether an agent asked for
  it; each opens as a receipt with code, image, hardware, energy, the agent turn, verdicts
  and a SHA-256 digest.
- **Agent runs**: each codex-task run as swimlanes (Claude, Codex, GPU jobs, people and
  system) and a step-through timeline.
- **Public window**: a shareable page built from an allowlist; the private notebook has a
  preview tab that shows exactly what it publishes.

## Settings

`~/.config/labbook/config.json` (or `$LABBOOK_CONFIG`). Every key is optional:

```json
{
  "electricity": {"price_per_kwh": 0.32, "currency": "USD",
                  "grid_gco2_per_kwh": 230, "grid_source": "your grid operator, year"},
  "public": {"title": "A one-person AI lab, live",
             "subtitle": "People and AI agents share two GPUs",
             "project_aliases": {"my-real-project": "Vision study"},
             "show_recent_receipts": 8,
             "repo_url": "https://github.com/ann-jin-dev/benchlight"}
}
```

Cost and CO₂ appear only when you set your own price and grid intensity. Record
locations default to the components' defaults and can be overridden with
`gpuq_state_dir`, `pulse_state_dir`, `codex_runs_dir` and `codex_sessions_dir`.

## Sharing the public window

`labbook public` binds 127.0.0.1 by default. Put it behind a tunnel you control, for
example `cloudflared tunnel --url http://127.0.0.1:8788` or `tailscale funnel 8788`.
Never expose `labbook serve`.

## Tests

```bash
python3 -m unittest discover -s labbook/tests -v
python3 labbook/tests/screenshot.py /tmp/shots http://127.0.0.1:8787/#/live   # needs geckodriver
```
