# 0008 Build the public window from an allowlist

- **Date:** September 2026 (approximate)

## Context

I want people to see the lab working without sharing private experiment
details. The private notebook joins commands, output paths, job names, agent
reports and other records. Copying that data and deleting sensitive fields
would make privacy depend on remembering every field that must be removed.

This is especially fragile when agents extend the record formats. A new field
can reach an existing export before anyone notices it contains private data.
I prefer a public document built from specifically chosen fields. Adding a
private field should not silently expand what the public window serves.

The current projection in [public.py](../../labbook/lab/public.py) constructs
GPU load, power, queue counts, aggregate totals and selected receipt fields.
Project names pass through owner-chosen aliases; unmapped names become
`private project`. The [public-window test](../../labbook/tests/test_labbook.py)
checks that its fixture's private name, path, command flag and token marker
are absent.

## Options considered

1. **Copy private records and delete sensitive fields.** This preserves most
   existing functionality with little extra projection code. Its default is
   to expose anything the deletion list does not recognize.
2. **Select public fields and serve them through the private server.** This
   controls the data shape. It still puts private and public routes in the
   same serving process and requires careful routing.
3. **Select public fields and use a separate public process.** This makes the
   public response and routes smaller and easier to inspect. It duplicates
   some serving work and still reads underlying private records locally.

## Decision

I build the public response field by field from an allowlist and serve it
through a separate public handler and process. Real project labels appear
only through aliases I choose.

## Consequences

New private fields are excluded until someone explicitly adds them to the
public projection. Private commands, paths, names, logs and transcripts do
not need a growing deletion list. The public handler serves only its own
page, assets and JSON endpoint; the notebook stays local. These routes are
implemented in [server.py](../../labbook/lab/server.py).

The cost is maintaining another representation of the data. New useful
public fields require deliberate work and review. I also lose the convenience
of publishing the full notebook with a few filters.

An allowlist cannot guarantee zero disclosure. An alias, title or other
permitted text can itself contain private information. Timing, GPU model,
load, energy and job outcomes can reveal activity patterns even when project
names are hidden. I accept those categories for this window, but they still
need review when research confidentiality requirements change.

A separate process reduces route mistakes; it is not operating-system
isolation. The process runs under the same account and reads private stores
to construct the projection. Exposing the correct port and choosing
safe aliases remain necessary.

## What would change this decision

I would delay or aggregate updates if live timing or activity became sensitive.
If the public service needed to run in an untrusted environment, it would
receive only prebuilt public records, with separate permissions for those
files.
