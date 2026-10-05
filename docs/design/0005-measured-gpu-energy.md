# 0005 Measure GPU energy and label CPU attribution

- **Date:** August 2026 (approximate)

## Context

I want to know how much energy an experiment used, alongside its results.
GPU-hours alone do not answer that question. Two jobs can occupy the same
card for the same length of time while drawing different amounts of power.
A fixed power assumption would hide that difference.

Exclusive GPU reservations make one part of the accounting straightforward.
Each card belongs to one queued job at a time. Its measured board energy can
therefore be assigned to that job without splitting it among concurrent queued
jobs. The CPU is different: several jobs and the operating system use the
same package. Its energy counter measures the package, not each job separately.

The distinction is implemented in [meter.py](../../pulse/meter.py). System
Pulse normally samples every five seconds and rejects intervals longer than
30 seconds for attribution. The [meter tests](../../pulse/test_meter.py) cover
exclusive-card accounting, CPU sharing, missing counters, gaps and reboots.

## Options considered

I went straight to the hardware's cumulative energy counters and did not
compare other methods at the time. Differences between counter readings capture
the energy the hardware reports over an interval. Counter resets, unavailable
sensors and sampling boundaries still limit coverage.

## Decision

I use GPU energy-counter differences for reserved cards and report metering
coverage. I attribute CPU package energy by each job's share of busy CPU time
and label it as attribution.

## Consequences

A reserved card's measured energy belongs to the job's reservation, including
idle board draw. It is not the energy of the training kernels alone, and it is
not the energy of the whole machine.

If a job accounts for half the busy CPU time, the rule assigns it half the
measured package energy for that interval. This is an accounting choice, not
a measurement of that job's physical CPU consumption. Different instructions,
frequencies and memory behavior can draw different power per CPU-second.

Even GPU attribution is not perfectly exact over the complete run. Jobs can
start or finish between samples. The meter needs observations at both ends
of an interval, and missing intervals remain unmetered. A reboot, counter
reset or long gap must not become a fabricated zero reading. Short jobs may
have little or no measured coverage. Energy comparisons need coverage checks
as well as totals, and whole-machine outlet estimates need their own labels.

## What would change this decision

I would add finer boundary measurements if short jobs regularly had inadequate
coverage. Allowing concurrent jobs on the same GPU would require a new
attribution policy. A supported external power meter would improve
whole-machine accounting without making CPU attribution exact.
