# Design decisions

Each record explains one decision behind Benchlight: the problem, the alternatives, the
choice, what it cost, and what would change it. The code shows what the system does; these
records explain why.

| # | Decision |
|---|---|
| 0001 | [Exclusive GPU reservations](0001-exclusive-gpu-reservations.md) |
| 0002 | [No backfilling around a ready job](0002-no-backfilling.md) |
| 0003 | [Keep failures visible and retries explicit](0003-no-automatic-replay.md) |
| 0004 | [Durable files between components](0004-files-between-components.md) |
| 0005 | [Measure GPU energy and label CPU attribution](0005-measured-gpu-energy.md) |
| 0006 | [Claude supervises and Codex executes](0006-supervisor-and-executor.md) |
| 0007 | [Bounded unattended wake-ups](0007-bounded-unattended-wakes.md) |
| 0008 | [Build the public window from an allowlist](0008-public-window-allowlist.md) |
| 0009 | [Keep the Python tools on the standard library](0009-python-standard-library.md) |
| 0010 | [A persistent view of the unattended workstation](0010-system-pulse.md) |
| 0011 | [Separate experiment environments and queue their execution](0011-docker-with-gpuq.md) |

The records can be read in any order. 0001–0003 cover how the queue shares GPUs, 0004–0005
how components share records and measure energy, 0006–0007 how the agents work together,
0008 what the public window reveals, 0009 the dependency policy, and 0010–0011 monitoring
and experiment environments.

New records start from [`0000-template.md`](0000-template.md). When a later decision
replaces an earlier one, the older record links to the newer one at the top.
