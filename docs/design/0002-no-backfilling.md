# 0002 No backfilling around a ready job

- **Date:** August 2026 (approximate)

## Context

My workstation has two GPUs. Some experiments need one; others need both at
the same time. If one card becomes free while a two-GPU job is waiting, it is
tempting to start a newer one-GPU job. Repeating that choice can keep the large
job waiting indefinitely. A policy that looks efficient at each dispatch can
be unfair over the whole queue.

I want the large job to have a turn even when agents keep submitting smaller
jobs. This means accepting an idle card while another running job finishes.
I do not want an agent to have to stop submitting useful work just to make a
two-GPU experiment possible.

Queue records from October 1, 2026 show the rule at work. At 3:31 a.m., one
card became free while a two-GPU job (record 1224) was waiting for both. The job
kept waiting for the second card, so the free card stayed idle for 14.6 minutes.
A one-GPU job at the same priority, submitted three seconds later (record 1225),
could have used that card. It waited instead and started only after the
two-GPU job finished. The policy itself is visible in [scheduler.py](../../gpuq/gpuqueue/scheduler.py). The
corresponding [scheduler test](../../gpuq/tests/test_queue.py) checks that a
newer small job waits behind a ready two-GPU request.

## Options considered

1. **Start any ready job that fits.** This keeps available resources busy.
   Without a reservation for the waiting large job, a stream of small jobs can
   keep using the card it needs next.
2. **Allow backfilling only when it cannot delay a reserved start.** This can
   improve utilization while protecting the large job. It needs useful runtime
   bounds and a more complicated reservation policy.
3. **Keep priority order, then submission order, without backfilling.** This
   makes the next turn predictable among equally prioritized ready jobs.
   Resources may remain idle while that job waits for its full request.

## Decision

I order ready jobs by priority, then submission order, and do not dispatch
around the first job when its resources are unavailable. Jobs still waiting
for dependencies do not block unrelated ready work.

## Consequences

A newer job at the same priority cannot continually take the spare GPU ahead
of a ready two-GPU job. This is a limited fairness rule. Higher-priority jobs
can still move ahead, and a continuous stream of those jobs can delay
lower-priority work. Existing jobs are not preempted.

The cost is deliberate idle time. A CPU or RAM shortage at the head of the
ready queue can also hold up a later job that would fit. This is acceptable
for a small workstation where I value understandable order, but it should
remain visible in wait reasons and telemetry. GPU utilization alone is not
enough to judge the policy; I also need to know whether experiments finish
in the intended order.

## What would change this decision

I would evaluate bounded backfilling if queue records repeatedly showed useful
work blocked for long periods behind a large request. I would first require
credible duration limits that protect the reserved job's start. Multiple users
would also require an explicit policy for priority and starvation.
