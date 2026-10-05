# 0009 Keep the Python tools on the standard library

- **Date:** August 2026 (approximate)

## Context

Agents modify experiment code and environments throughout the day. The queue,
telemetry agent and notebook need to remain available while those experiments
change. I keep the dependency surface small so that operating the workstation
stays separate from maintaining each research environment.

For example, the notebook uses Python's `http.server`, SQLite and JSON support.
Its browser charts are small JavaScript and SVG routines, rather than a chart
package installed through a frontend build system. These choices are visible
in [server.py](../../labbook/lab/server.py),
[sources.py](../../labbook/lab/sources.py) and
[chart.js](../../labbook/web/chart.js).

## Options considered

1. **Use web and telemetry frameworks.** They provide mature routing, data
   handling and visualization features. They also add package versions,
   transitive dependencies and an update process to the workstation tools.
2. **Use a separate, pinned application environment.** This isolates the tools
   from experiment dependencies and allows external libraries. It still needs
   installation, upgrades and compatibility checks for that environment.
3. **Use the Python standard library and small browser scripts.** This keeps
   the tools easy to inspect and run with a supported Python installation.
   It requires maintaining code for features a library would otherwise supply.

## Decision

I keep the local Python coordination and notebook tools free of third-party
Python packages. Experiments retain their own dependencies
inside separate environments, preferably Docker images.

## Consequences

Installing a new research package does not become a prerequisite for viewing
the notebook or reading queue records. The CPU-only demo requires Python
3.10 or later and no additional Python packages. Avoiding a frontend package
build also makes it easier to inspect the files the browser actually loads.

The cost is code I must own. HTTP handling, caching, charts, date formatting
and interactions need testing and maintenance. The standard library does not
automatically provide the accessibility, deployment features or performance
of a specialized framework. Small custom implementations can still contain
errors, and Python itself still needs maintenance.

This is a scoped dependency rule. The full workstation depends on Linux,
systemd, GPU drivers, Docker, NVIDIA tooling and the coding-agent clients.
Browser code uses JavaScript. Research workloads use external Python packages.
The small HTTP server is also not a complete authentication or multi-user
application platform; those remain outside the current notebook's scope.

## What would change this decision

I would add a carefully scoped dependency if a required feature became
costlier or less safe to maintain myself. Authenticated multi-user access,
substantially larger data views or complex accessible chart interactions
would justify evaluating a framework and an isolated application environment.
