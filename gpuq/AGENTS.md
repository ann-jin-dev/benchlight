# GPU queue development

This is a standard-library Python queue for one account on one workstation.
Read README.md before changing the scheduling policy or execution model.

- Persist GPU/CPU/RAM reservations before external runtime creation.
- Preserve uncertain active reservations and stop new dispatches on host errors.
- Use stable GPU UUIDs and validate ownership before stopping/removing runtimes.
- Docker and native systemd jobs must outlive worker restarts. Do not spawn jobs
  directly inside the worker's service cgroup.
- Preserve failed attempts and assign a new job ID for every retry.
- Keep environment values and credentials out of reports and command output.
- Run `python3 -m unittest discover -s tests -v` for scheduling/recovery changes.
- Run `tests/host_smoke.py` through the approved host path only with an idle queue
  and an existing compatible Torch image. Cleanup must affect only its own jobs.
