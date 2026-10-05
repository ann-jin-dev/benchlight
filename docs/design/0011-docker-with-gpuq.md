# 0011 Separate experiment environments and queue their execution

- **Date:** August 2026 (approximate)

## Context

Different experiments need different packages and CUDA userspace libraries.
Agents may install or change them independently. If experiments share one
mutable environment, preparing the next job can change what an earlier job
will run with. I want environment changes to stay local to the experiment
that needs them.

Docker addresses that environment boundary. gpuq addresses a separate problem:
which job may use the workstation's GPUs, CPUs and RAM, and when. Putting
two jobs in separate containers does not prevent them from choosing the same
physical GPU. Using both tools gives me environment separation and a common
place to coordinate execution. The workflow is described in the
[Docker guide](../../docker/README.md) and [queue guide](../../gpuq/README.md).

## Options considered

1. **Use one shared Python environment and direct launches.** This is easy to
   start with and avoids image builds. Package changes and resource allocation
   need coordination across every experiment using it.
2. **Use separate Docker images through gpuq.** Each experiment supplies its
   userspace environment while the queue assigns resources and records the
   runtime. Image builds, storage and mount management add work.

## Decision

I prefer a separate Docker image for each experiment environment and route
GPU execution through gpuq. I pin dependencies and record the resolved image
ID; when source must stay fixed while queued, I use a frozen source snapshot.

## Consequences

Changing one experiment's image does not require changing another's packages.
Read-only code and data mounts, separate run outputs and recorded physical GPU
assignments make it easier to understand what ran. CPU and RAM limits also
reduce accidental resource interference between concurrent jobs.

The cost is maintaining images and launch descriptions. Large images consume
disk space, builds take time, and incorrect mounts can still make a valid
image fail. The template asks me to choose pinned package versions; it does
not choose or freeze those versions automatically.

A mutable image tag is resolved when the job starts. If I need the environment
fixed at submission, I pass an immutable image ID or digest. Recording the
resolved ID afterward explains what ran but does not prevent a tag from
changing during the wait. Without a source snapshot, queued jobs likewise see
later edits to their submitted directory.

Containers still share the host driver and kernel. External inputs, writable
caches and nondeterministic GPU operations can change results. This design
reduces environment mismatch; it does not guarantee reproducible scientific
results by itself. Native jobs remain supported when a project's established
protocol requires them, but they need the same provenance discipline.

## What would change this decision

I would use another isolated runtime if Docker could not support a required
library or execution protocol. Moving to a shared cluster would require its
scheduler and container format, while retaining fixed environments, explicit
resource requests and separate attempt records.
