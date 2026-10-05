# 0006 Claude supervises and Codex executes

- **Date:** August 2026 (approximate)

## Context

I need help with both deciding what an experiment should answer and carrying
out a well-defined task. In my experience, Claude explains experimental ideas
and results in a way I find easier to follow. Codex handles many clearly
bounded implementation tasks well, but I often find its reports harder to
understand. I want that difference reflected in the workflow.

I choose the execution model for its capability and cost. The model is
configurable; the supervisor-executor design does not depend on one model
version.

The bridge provides a concrete handoff: a written brief, a visible Codex
thread, a final report, and a supervisor verdict. The report has five sections
and ends with `STATUS: DONE`, `QUESTION` or `BLOCKED`. The contract and
verification instructions are in the
[bridge skill](../../codex-bridge/skill/SKILL.md).

Two reviews show what this catches. On September 30, 2026, in a measurement
study, Claude found that 9 or 10 of the 12 sampled sources in each pilot prompt
were filler, so distractors almost never appeared and the test would have been
too easy. On October 1, Codex reported that three model variants for an
ablation were matched in size, within 4.2% in parameters. Claude read the code
and found that the two arms meant to differ by one component had different
internal widths, 88 and 64, which would have confounded the comparison.
Codex's reports mentioned neither problem, and both were fixed before any
scoring or training. In six other reviews, Claude recomputed the reported
numbers independently and they matched.

## Options considered

1. **Use Codex for the whole loop.** This reduces handoffs and lets the same
   agent keep implementation context. It leaves the communication difficulty
   I described for me to resolve directly.
2. **Use Claude for both planning and execution.** This gives me one continuous
   conversation. It gives up the division of work I prefer for repeated,
   bounded execution tasks; its actual cost would need measurement.
3. **Let Claude supervise Codex.** Claude translates my goals into briefs,
   checks results and explains the conclusion. This adds another model call
   layer and creates opportunities for misunderstandings during handoffs.

## Decision

I use Claude to discuss direction, write briefs, verify artifacts and explain
results, while Codex executes the brief. I retain decisions about scope and
the interpretation of research results.

## Consequences

The brief makes the question, constraints, decision rule and expected artifacts
explicit. Claude waits through the bridge, then checks source files and numbers
before recording a verdict. A clearer explanation is useful only if it stays
faithful to the evidence; it cannot repair a wrong result by sounding confident.

Two agents can share the same blind spot. Verification must inspect actual
artifacts rather than accept the executor's summary. Handoffs add latency,
tokens and integration maintenance. The supervisor can also omit a constraint
or smooth away uncertainty, so the original report remains available.

I require executor threads to remain visible in the app. The bridge uses
the [Codex app-server thread protocol](https://learn.chatgpt.com/docs/app-server)
and keeps briefs, reports and notes for the notebook's timeline. This makes
the work inspectable.

## What would change this decision

I would simplify the workflow if one agent consistently produced explanations
I could understand, accurate execution and comparable verified results at
lower total cost. Repeated handoff errors or an inability to keep executor
threads visible would also make me reconsider the bridge.
