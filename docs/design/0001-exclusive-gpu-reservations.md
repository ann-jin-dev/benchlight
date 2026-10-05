# 0001 Exclusive GPU reservations

- **Date:** August 2026 (approximate)

## Context

I run research experiments on a workstation with two RTX 5080 GPUs. More than
one agent can ask for compute. If they each inspect GPU usage and launch a job,
they can both choose the same card. A quiet card is an observation, not a
reservation. Sharing also makes each job's memory use and execution time depend
on another experiment.

My concern includes the agent's work while waiting. Repeatedly asking an agent
to check whether a GPU is free consumes turns and introduces a delay between
checks. I want to submit work once and let a persistent scheduler start it when
resources become available.

A host check on September 28, 2026 confirmed the behavior: two single-GPU jobs
ran at the same time on separate cards, then one two-GPU job reserved both. The
reservation rule is in [scheduler.py](../../gpuq/gpuqueue/scheduler.py); its
separate-card behavior is covered by [test_queue.py](../../gpuq/tests/test_queue.py).

## Options considered

1. **Let each agent inspect and launch directly.** This needs little shared
   infrastructure. It leaves waiting and coordination to the agents, and two
   successful checks can still lead to conflicting launches.
2. **Reserve whole GPUs in one queue.** A job receives a stable physical GPU
   UUID until its runtime stops. This is easy to reason about, but a small job
   can occupy a whole card.

## Decision

I use one persistent queue with exclusive reservations by physical GPU UUID.
A two-GPU request acquires both cards together.

## Consequences

The agent can enqueue work and use a blocking wait instead of repeatedly making
launch decisions. The scheduler still checks the host periodically, so starts
have a finite dispatch delay. Exclusivity also makes whole-card GPU energy
attribution simpler.

The cost is unused capacity within a reserved card. A job using a small part of
its memory still prevents another queued job from using that GPU. CPU, memory
bandwidth and storage remain shared and can still affect timing. Reservations
coordinate queue submissions; they cannot stop an independent program from
launching directly. I therefore route research launches through gpuq and retain
its checks for unmanaged work.

## What would change this decision

I would revisit sharing if representative small jobs routinely left substantial
capacity unused and controlled measurements showed that sharing preserved their
memory safety and timing requirements.
