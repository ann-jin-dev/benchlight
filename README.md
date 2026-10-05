# Benchlight

**Keeping AI-assisted research traceable, double-checked, and under human control.**

Researchers increasingly hand parts of their work to AI assistants, which write code, run
experiments on powerful computers, and report back. Benchlight makes sure that work can
still be trusted. Every experiment is traceable: it gets a receipt, much like a store
receipt, recording what was run, on which hardware, how much electricity it used, and which
AI asked for it. Every result is double-checked: a second AI verifies the first one's work
against the raw data before it is reported. And people stay in control: the AI works only
within limits a person has approved, and decisions are recorded alongside the results. A
public web page shows the computer at work without revealing private research.

In six days of daily use, earlier versions of these tools handled about 1,600 computing
jobs, and the checking AI caught two flaws in experiment designs before those experiments
were run.

![The public page: the research computer's processors at work right now, its power draw, and the energy used each day](docs/images/public-window.png)

## How it works

Benchlight runs on a Linux machine with NVIDIA GPUs that people and coding agents (Claude
Code, OpenAI Codex or your own scripts) share. Agents submit GPU jobs to a queue instead of
grabbing cards. Every job gets a **receipt**: the exact code, container image, GPUs,
measured energy, and the agent turn that asked for it. Agent runs can be **replayed** step
by step, and an optional **public window** shows the machine working without exposing
anything private.

## Key ideas

- **Energy is measured per job, not modelled.** The queue reserves whole GPUs, so the change
  in a card's energy counter while a job holds it belongs to that job alone. Receipts also
  say how much of each run was metered.
- **Provenance reaches the agent's turn.** Each job links to the Codex turn that submitted
  it, with that turn's tokens and the verdicts recorded on it. A SHA-256 digest covers
  everything that cannot change after the job ends, so anyone can check a copy.
- **One agent checks another.** Claude writes the brief, Codex executes it, and Claude
  verifies the result against the raw files before anything is reported. Every step stays
  visible and is kept for replay.
- **Unattended agents have hard limits.** Wake-ups without a person present run with a narrow
  tool allowlist, a per-run budget and a kill switch, and failed jobs are never rerun
  automatically.
- **The public view is private by construction.** It is built from an allowlist of fields,
  so a new private field can never leak by default.

## In practice

Benchlight grew out of daily use on a two-GPU research workstation. Over six days,
September 28 to October 3, 2026, earlier versions of these tools ran **1,586 GPU jobs**
(1,338 succeeded) for 39 projects, about **124 GPU-hours** in total. Claude supervised
**34 Codex turns** across 8 agent runs. In that work, Claude's review caught two
experiment-design errors that Codex's reports had not mentioned, both before the affected
runs started; in six other reviews, its independent recomputation matched Codex's numbers.
The [design records](docs/design) explain each main decision: the problem, the options,
the choice, its costs, and what would change it.

## Is it for you?

Benchlight fits if:

- you have one Linux workstation or rented server with one or more NVIDIA GPUs;
- coding agents (Claude Code, OpenAI Codex or your own scripts) launch experiments on it,
  sometimes while nobody is watching;
- you want to know afterwards exactly what ran, on which GPU, what it cost, and who asked
  for it.

It is not a cluster scheduler. It manages one machine and one user account.

## Try it in ten seconds (no GPU needed)

```bash
git clone https://github.com/ann-jin-dev/benchlight && cd benchlight
python3 labbook/labbook demo
```

Open <http://127.0.0.1:8787> for the private notebook and <http://127.0.0.1:8788> for the
public window. The demo writes an invented week of records in the same formats the real
tools produce. It needs only Python 3.10 or later.

## Use only what you need

Each tool works on its own, and the Python tools need nothing beyond the standard library.
Installed together, they link up: labbook joins the records the others write.

| Tool | Use it when | Needs | Start with |
|---|---|---|---|
| [`gpuq/`](gpuq) | Several people or agents launch GPU jobs on one machine | systemd; Docker for container jobs | `python3 gpuq/install.py` |
| [`pulse/`](pulse) | You want temperatures, load, power and per-job energy | `nvidia-smi` for GPU readings | `python3 pulse/agent.py --once` |
| [`labbook/`](labbook) | You want receipts, run replay and a dashboard | Records from any of the other tools | `python3 labbook/labbook demo` |
| [`codex-bridge/`](codex-bridge) | Claude Code should brief and check OpenAI Codex | Claude Code and the Codex CLI | [codex-bridge/README.md](codex-bridge/README.md) |
| [`docker/`](docker) | Each experiment needs its own CUDA environment | Docker and the NVIDIA Container Toolkit | `bash docker/new-experiment.sh my-study` |
| [`remote-control/`](remote-control) | You want to reach the machine from claude.ai or the Claude app | Claude Code | [remote-control/README.md](remote-control/README.md) |

```mermaid
flowchart LR
  person([Person]) -- decisions --> claude[Claude Code<br/>supervisor]
  claude -- brief / resume / steer --> bridge[codex-task]
  bridge -- app-server thread --> codex[Codex<br/>executor]
  codex -- gpuq submit<br/>+ CODEX_TASK_RUN --> gpuq[(gpuq)]
  claude -- gpuq submit --> gpuq
  gpuq -- exclusive GPUs --> jobs[Docker / native jobs]
  pulse[System Pulse] -- NVML + RAPL energy --> energy[(energy meter)]
  gpuq -. status .-> pulse
  bridge -. runs, notes, tokens .-> labbook[labbook]
  gpuq -. jobs, manifests .-> labbook
  energy -. per-job Wh .-> labbook
  labbook --> private[Private notebook<br/>127.0.0.1]
  labbook --> public[Public window<br/>allowlist only]
```

The tools talk through files, not network services: each keeps its own records, and labbook
reads them. See [docs/architecture.md](docs/architecture.md).

## Install everything

Requirements: Linux with systemd, an NVIDIA driver, Docker with the NVIDIA Container
Toolkit, and Python 3.10 or later. On Ubuntu, `docker/setup-host.sh` sets up Docker and the
toolkit.

```bash
./install.sh --check            # check prerequisites only
./install.sh                    # install and start the services
loginctl enable-linger "$USER"  # keep services running after logout
```

Everything installs as systemd **user** services, and the installer never replaces a file it
did not create. Check the result with `gpuq status`, `labbook open`, and
`systemctl --user status gpuq system-pulse-agent labbook`. Run `./install.sh --help` for
all options, including `--with-remote-control DIR`.

## Make it fit your machine

Everything below is optional. Without it, Benchlight still runs and says what it cannot
measure.

| What | Where | Without it |
|---|---|---|
| CPU and RAM that queued jobs share | `--max-cpus` and `--max-memory` when installing | All but 4 CPU cores (at most 16) and all but 6 GiB of RAM (at most 24 GiB) |
| Hardware profile for the outlet power estimate | Copy [`pulse/profiles/x870-dual-rtx5080.json`](pulse/profiles/x870-dual-rtx5080.json) to `~/.config/system-pulse/profile.json` and edit it for your parts, or pass `--profile` to the installer | A generic profile with a wider estimate |
| Electricity price and grid carbon intensity | `electricity` in `~/.config/labbook/config.json` | Receipts show energy in Wh, with no cost or CO₂ |
| Names shown in the public window | `public.project_aliases` in the same file | Every project appears as "private project" |
| CPU energy | Read access to the RAPL counter, granted by the udev rule in [`pulse/system/`](pulse/system) | Receipts show GPU energy only |
| Codex model and reasoning effort | `CODEX_TASK_MODEL` and `CODEX_TASK_EFFORT` | Codex's own configuration |

## Receipts

<img src="docs/images/receipt.png" alt="A receipt for one training job" width="520" align="right">

Every gpuq job gets a receipt that answers: *what exactly ran, on what, what did it cost,
and who asked for it?*

- **Code**: the Git commit at submission, or a frozen source snapshot reduced to one tree
  hash.
- **Environment**: the immutable Docker image ID the job actually started with.
- **Hardware**: the physical GPUs (by UUID) and the CPU and RAM reserved.
- **Energy**: GPU energy is *measured*, not estimated. gpuq reserves cards exclusively, so
  the change in a card's NVML energy counter while a job holds it is that job's energy.
  CPU energy is attributed from the RAPL package counter by the job's share of busy CPU
  time. The receipt states how much of the run was metered.
- **Cost and CO₂**: computed only from the electricity price and grid intensity you set;
  never guessed.
- **Agent**: the Codex run and turn that submitted the job, its token usage, and the
  verdicts and decisions recorded on that turn. Links are marked *exact* (recorded at
  submission) or *inferred* (from time and project folder).
- **Digest**: a SHA-256 over the parts that cannot change once the job has ended (job,
  times, code, environment, hardware, energy). Anyone with a copy can check it with
  `labbook verify receipt.json`; notes added later and the tariff stay outside it.

`labbook receipt <job id>` prints one in the terminal; the notebook renders it as above.

<br clear="right">

## Run replay

![Replaying an agent run: Claude's briefs, Codex's turns, the GPU jobs they launched, and the verdicts and decisions recorded along the way](docs/images/replay.png)

An agent run is one Codex thread supervised by Claude. The replay puts every actor on one
timeline: Claude's brief and instructions, each Codex turn with its report and token count,
the GPU jobs launched in that turn, unattended wake-ups, Claude's verification verdicts and
the person's decisions. A slider and a play button step through it in order.

## Public window

`labbook public` serves a page you can share: GPUs right now, power draw, jobs running,
lifetime totals, energy per day and recent receipts. It runs as a separate process that can
serve nothing else, and its data is built from an **allowlist**: no hostnames, paths,
commands, job names, logs or transcripts. Project names appear only under aliases you
choose. Expose only this port, for example through a tunnel.

## Safety model

Agents get real compute, so the guardrails are part of the design. GPUs are reachable only
through the queue, which refuses to schedule onto cards with unmanaged processes;
reservations are saved before anything starts; failed jobs are never rerun automatically;
agent threads stay visible to a person; unattended wake-ups run with a narrow tool
allowlist, a per-run budget and a kill switch; and nothing listens on a public interface
except the allowlisted window. Details and trade-offs: [docs/safety.md](docs/safety.md).

## Why it works this way

Each main choice has a short design record in [docs/design/](docs/design): why GPUs are
reserved whole, why the queue never lets small jobs jump ahead, why failures are never
retried automatically, why the tools share files instead of services, and more. Read the
relevant one before changing a policy.

## Running the tests

If you adapt Benchlight, these are the same checks CI runs:

```bash
(cd gpuq && python3 -m unittest discover -s tests -p 'test_*.py')
(cd pulse && python3 -m unittest test_meter test_monitoring)
python3 -m unittest discover -s labbook/tests -p 'test_*.py'
bash tests/install_test.sh
shellcheck --severity=warning install.sh docker/*.sh remote-control/headless-display/virtual-display.sh tests/*.sh
```

The Python tools use only the standard library
([design record 0009](docs/design/0009-python-standard-library.md)).

## Contributing

Issues are welcome: bug reports, questions, and notes on how you use or adapt Benchlight.
Pull requests are not being accepted for now. Please report security issues privately, as
described in [SECURITY.md](SECURITY.md).

## Status and limits

- One machine and one user account. No multi-user authorization or remote job submission.
- NVIDIA GPUs, systemd and Docker only. Developed on Ubuntu 24.04 with two RTX 5080 cards,
  where earlier versions of these tools have run daily since September 2026.
- CPU energy is an attribution, not a measurement, and needs read access to RAPL.
- Agent links are exact for jobs submitted through `codex-task` runs (or with
  `CLAUDE_CODE_SESSION_ID` set); jobs submitted by other scripts are linked by inference and
  labelled as such.

## AI use

Benchlight was authored by Ann Jin with the help of AI tools like Claude and Codex.

## License

MIT. See [LICENSE](LICENSE).
