# 0010 A persistent view of the unattended workstation

- **Date:** September 2026 (approximate)

## Context

I ask agents to run GPU experiments throughout the day, including when I am
away. Once I chose that workflow, I also needed a way to see whether the
workstation was healthy and using its resources as intended. Automatic
completion or failure messages did not answer that ongoing question.

From my phone or another terminal, I want to check GPU utilization, CPU
temperature, fan speeds and the gpuq queue. A GPU with low utilization can
mean an input bottleneck, an idle interval or a job waiting for another
resource. A queue report alone cannot distinguish all of those cases.
Temperature and fan readings answer a different question again: whether the
machine is operating within the conditions I expect.

[System Pulse](../../pulse/README.md) combines those categories in one local
recording agent. Its default interval is five seconds, and its history keeps
minute summaries for twelve hours.

Pulse found a problem I was not looking for. On September 28, 2026, its
twelve-hour history showed the CPU running hotter whenever the GPUs were busy.
At the same CPU power, about 77 W, the CPU averaged 83.5 °C in minutes when the
two GPUs together drew over 400 W, and 72.4 °C when they drew 100–400 W. The
highest reading that day was 93.4 °C, close to the processor's 95 °C limit.
The CPU cooler was drawing in hot air exhausted by the GPUs. I changed the case
airflow, and the CPU stopped heating up with GPU load.

## Options considered

1. **Rely on completion and failure notifications.** This needs little extra
   interaction when jobs behave as expected. It gives no continuous view of
   load, temperature or queue waiting conditions.
2. **Inspect each tool remotely when needed.** GPU utilities, sensor readings
   and queue commands provide detailed answers. Reassembling the picture on
   a phone is inconvenient, and it lacks shared historical context.
3. **Run a persistent telemetry agent with a dashboard.** One place can show
   hardware readings and queue state over time. This adds collection overhead,
   storage, sensor permissions and another component to maintain.

## Decision

I use System Pulse to record hardware health, resource use and queue state
continuously. I want remote visibility as well as event notifications, with
private details kept separate from the public window.

## Consequences

The dashboard makes it easier to connect waiting jobs with actual resource
conditions. History helps distinguish a momentary reading from a sustained
pattern. Temperature alerts add another signal, but I still need the current
view when nothing has triggered an alert.

Collection has a cost. Each sample invokes host observations and updates local
records. Sensors can be missing, inaccessible or misleading, so unavailable
data must remain unavailable rather than appear as zero. A five-second sample
interval also cannot capture every brief spike.

High GPU utilization is not proof of useful research progress. A repeated or
incorrect experiment can keep both GPUs busy. I need to interpret telemetry
alongside job logs, outputs and the experiment's question. Policy-driven idle
time, such as waiting for a two-GPU job, can also be intentional.

Remote viewing needs an appropriate access route; a local agent alone does
not make a phone dashboard reachable. Hosted snapshot uploads are optional.
Their data needs its own privacy review; the labbook public allowlist does
not automatically sanitize a different upload path.

## What would change this decision

I would simplify collection if its overhead interfered with experiments or
its readings were rarely useful. Multiple workstations would require a common
authenticated view. Sensitive activity patterns could require coarser or
delayed remote telemetry.
