# 0007 Bounded unattended wake-ups

- **Date:** August 2026 (approximate)

## Context

My workstation runs experiments while I am away. A coding-agent turn can
finish after its supervising session has exited. If a result needs checking
before an already planned next step, progress can stop even though the machine
and GPUs remain available.

I want unattended wake-ups to continue work I have already authorized, with
enough limits to stop when the next action needs my judgment.
Availability of compute does not authorize a new experiment. I want to review
what happened later, including why the supervisor woke or remained stopped.

The conditions are implemented in
[codex-task](../../codex-bridge/bin/codex-task), and the authorized-scope rule
is in the [bridge skill](../../codex-bridge/skill/SKILL.md).

## Options considered

1. **Wait for me after every completed turn.** This gives me direct control
   over every continuation. It can leave an approved workflow idle for hours
   simply because I am unavailable.
2. **Keep the supervisor running continuously.** It can handle completions
   without launching a replacement session. It still needs limits and a way
   to recover when that session disappears.
3. **Wake the supervisor on completion within a bounded policy.** This lets
   the established loop resume after the original session exits. It requires
   explicit checks against duplicate supervision and indefinite continuation.

## Decision

I keep completion-triggered, bounded supervisor wakes for already authorized
work. When a continuation needs a new user decision,
the supervisor stops and reports the question.

## Consequences

Each condition answers a particular concern. A live waiter handles completion
within the existing session; a live supervisor without a waiter does not
trigger a second supervisor. A wake budget limits repeated launches. The
global halt flag prevents future unattended wakes, and wake logs make the
wrapper's decisions inspectable.

The headless supervisor uses `dontAsk` mode with a tool allowlist for reading
records, inspecting queue status and operating the bridge. It does not
automatically approve a request that needs a person. However, the allowed
bridge can resume an executor with broader workspace capabilities. The
allowlist therefore limits direct supervisor tools, not every action the
executor might take. Staying within approved scope also depends on the brief
and agent instructions.

The cost is unattended activity, additional model use and more recovery logic.
A wake limit is not a total token, time or GPU-hour budget. One wake can start
substantial work. Halting wakes also does not stop an existing executor turn
or GPU job; those require the separate stop or cancel controls documented in
[safety.md](../safety.md).

## What would change this decision

I would restrict unattended continuation if reviews found it repeatedly
crossing approved scope or producing little useful progress. A need for hard
spending limits would require enforced budgets, beyond counting wakes.
