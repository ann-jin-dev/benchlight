# 0003 Keep failures visible and retries explicit

- **Date:** August 2026 (approximate)

## Context

A failed experiment can tell me that the environment, inputs or command are
wrong. If the queue silently runs it again, the supervising agent may see only
the eventual success. That makes it harder to understand what happened and
can spend more GPU time before anyone investigates the cause.

I want a failure to remain part of the experiment record. An apparent
transient error may reveal something persistent. Repeating the same command
is also unsafe when it writes to external files or has other effects that
are not automatically reversible. A fresh attempt needs its own identity.

Queue records from September 28, 2026 show what happens instead. At 9:44 p.m.,
a test job failed with exit code 1, and the job that depended on it was skipped
rather than run. About a minute and a half later, the same tests were submitted
as a new job and passed. The skipped job was then retried with `gpuq retry`,
its dependency pointed at the new test job, and it succeeded. All four records
remain in the queue.

## Options considered

1. **Automatically replay every failed job.** This can recover from temporary
   interruptions without human attention. It can repeat a deterministic error
   or duplicate effects without diagnosing the failure.
2. **Record the failure and require an explicit new attempt.** The person or
   supervising agent decides what to do next. Progress may pause while nobody
   is available to make that decision.

## Decision

I never automatically replay a failed job. An explicit `gpuq retry` creates a
new job ID and output directory, links it to the previous attempt, and keeps
the original record.

## Consequences

The failure stays available to the supervisor and the lab notebook. Retry
provenance belongs to whoever requests the retry, rather than being copied
from the original submitter. If the original job recorded a Docker image ID,
the retry reuses it. Existing source snapshots are also retained.

The cost is slower recovery from temporary failures. The agent must inspect
the error, decide whether the same configuration remains appropriate, and
explicitly request another attempt. `retry` repeats the saved configuration;
fixing the command or environment may require a new submission instead.
Dependent jobs whose prerequisite fails are skipped, so repairing a workflow
may also require correcting its dependency IDs.

The implementation and retry-origin test are in
[cli.py](../../gpuq/gpuqueue/cli.py) and
[test_queue.py](../../gpuq/tests/test_queue.py).

## What would change this decision

I would consider an opt-in retry policy for a narrowly identified temporary
failure, provided the workload was safe to repeat. Every attempt would still
need separate records, a retry limit and a compute budget; eventual success
would not erase the original failure.
