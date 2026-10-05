# 0004 Durable files between components

- **Date:** August 2026 (approximate)

## Context

The workstation runs while I am away. Its queue, telemetry agent, coding-agent
bridge and notebook do not always stop or restart together. I want each piece
to recover its view from saved records, without needing another component to
be online at exactly the same moment.

This also matters after a power failure. Durable files preserve queue entries,
logs and available results. They cannot restore a training process that lost
its memory when the machine stopped. Recovery has to distinguish rebuilding
system state from repeating an experiment. Otherwise it would conflict with
my decision to keep retries explicit.

The saved host verification on September 28, 2026 reports that worker restart
and forced termination preserved Docker and native job processes, reservations
and logs. The current implementation is described in
[architecture.md](../architecture.md) and [the queue guide](../../gpuq/README.md).

## Options considered

1. **Use a durable message broker.** Components could react to events and
   replay them after an outage. This adds another service and requires decisions
   about delivery, duplicates and the source of current state.
2. **Let each component keep records that others read.** SQLite databases and
   JSON files are inspectable with ordinary tools. Readers can see stale data
   and need to understand each record format.

## Decision

I connect the local components through durable records with clear ownership.
Components read each other's records; they do not edit them through the
notebook or require a message service between them.

## Consequences

The queue owns its job state, Pulse owns telemetry and energy records, and the
bridge owns agent-run records. The notebook opens SQLite in read-only mode
with `query_only` enabled. Queue JSON writes use a temporary file, flush it,
and replace the destination. This prevents readers from seeing half-written
JSON during normal operation.

There is no transaction covering all components. A receipt can temporarily
combine records observed at different times.

A restarted worker adopts surviving runtimes. After a machine reboot, a
missing runtime is recorded as a failure and needs an explicit retry. Pending
jobs remain queued. The cost of this design is format coupling and eventual
updates, rather than immediate cross-component consistency. The read and write
mechanisms are in [sources.py](../../labbook/lab/sources.py) and
[store.py](../../gpuq/gpuqueue/store.py).

## What would change this decision

I would revisit the boundary when a second machine needs authenticated access
to live records. Shared filesystem access is not a multi-machine coordination
protocol. A requirement for immediate, acknowledged events would also justify
evaluating an API or durable event service.
