"""labbook command line."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser

from .config import config_path, load_config
from .core import Lab


def print_receipt(receipt: dict) -> None:
    job, when, energy, agent = receipt["job"], receipt["times"], receipt["energy"], receipt["agent"]
    line = "─" * 52
    print(line)
    print(f"RECEIPT {receipt['id']}   {job['status'].upper()}")
    print(line)
    print(f"{job['project']} / {job['name']}")
    if when["duration_seconds"] is not None:
        print(f"ran {when['duration_seconds'] / 60:.1f} min after {when['queue_wait_seconds'] or 0:.0f} s in queue")
    code = receipt["code"]
    if code["git_commit"]:
        print(f"code   {code['git_commit'][:12]}{' (dirty)' if code['git_dirty'] else ''}")
    if code["snapshot"] and code["snapshot"].get("tree_sha256"):
        print(f"source {code['snapshot']['files']} files, tree {code['snapshot']['tree_sha256'][:16]}")
    environment = receipt["environment"]
    if environment["image_id"]:
        print(f"image  {environment['image_id'][:19]}")
    for gpu in receipt["hardware"]["gpus"]:
        print(f"gpu    {gpu['name']} ({gpu['uuid']})")
    if energy:
        cpu = f" + CPU {energy['cpu_wh']:.1f} Wh" if energy["cpu_wh"] is not None else ""
        coverage = f" ({energy['coverage']:.0%} of the run metered)" if energy["coverage"] is not None else ""
        print(f"energy GPU {energy['gpu_wh']:.1f} Wh{cpu}{coverage}")
        pricing = receipt.get("pricing") or {}
        if "cost" in pricing:
            print(f"cost   {pricing['cost']['amount']:.4f} {pricing['cost']['currency']}")
        if "co2_grams" in pricing:
            print(f"CO2    {pricing['co2_grams']:.0f} g")
    else:
        print("energy not recorded (System Pulse was not metering this job)")
    if agent and agent.get("run"):
        tokens = (agent.get("turn_tokens") or {}).get("total_tokens")
        print(f"agent  {agent['run']} turn {agent['turn']} ({agent['link']})"
              + (f", {tokens:,} tokens that turn" if tokens else ""))
        for note in agent.get("notes", []):
            print(f"  {note.get('by')} {note.get('kind')}: {note.get('text', '')[:70]}")
    elif agent and agent.get("claude_session"):
        print(f"agent  Claude Code session {agent['claude_session']} (exact)")
    print(line)
    print(f"digest {receipt['digest'] or '(final when the job ends)'}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="labbook", description="Receipts, run replay and live views "
                                     "for an AI research workstation")
    commands = parser.add_subparsers(dest="action", required=True)
    serve = commands.add_parser("serve", help="private notebook on 127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    public = commands.add_parser("public", help="public window server (expose only this one)")
    public.add_argument("--host", default="127.0.0.1")
    public.add_argument("--port", type=int, default=8788)
    opener = commands.add_parser("open", help="open the private notebook in a browser")
    opener.add_argument("--port", type=int, default=8787)
    receipt = commands.add_parser("receipt", help="print one job's receipt")
    receipt.add_argument("job_id", type=int)
    receipt.add_argument("--json", action="store_true")
    commands.add_parser("runs", help="list agent runs")
    replay = commands.add_parser("replay", help="print an agent run's timeline")
    replay.add_argument("run_id")
    verify = commands.add_parser("verify", help="check a receipt JSON file against its digest")
    verify.add_argument("file")
    commands.add_parser("public-json", help="print what the public window would show")
    commands.add_parser("config", help="show settings and where they come from")
    demo = commands.add_parser("demo", help="serve both views on invented records (no GPU needed)")
    demo.add_argument("--dir", help="where to write the demo records (default: a new temp folder)")
    demo.add_argument("--port", type=int, default=8787)
    demo.add_argument("--public-port", type=int, default=8788)
    args = parser.parse_args(argv)

    if args.action == "verify":
        from .receipts import DIGEST_SCOPE, verify as check
        receipt = json.loads(open(args.file).read())
        if check(receipt):
            print(f"OK: {receipt['id']} matches its digest ({', '.join(DIGEST_SCOPE)})")
            return 0
        print(f"MISMATCH: {receipt.get('id')} was changed after it was issued, or is not final", file=sys.stderr)
        return 1

    if args.action == "demo":
        from .demo import serve as serve_demo
        serve_demo(args.dir, args.port, args.public_port)
        return 0

    try:
        lab = Lab(load_config())
    except (ValueError, OSError) as error:
        print(f"labbook: {error}", file=sys.stderr)
        return 2

    if args.action == "serve":
        from .server import PrivateHandler, run
        run(PrivateHandler, lab, "127.0.0.1", args.port)
    elif args.action == "public":
        from .server import PublicHandler, run
        run(PublicHandler, lab, args.host, args.port)
    elif args.action == "open":
        url = f"http://127.0.0.1:{args.port}/"
        print(url)
        webbrowser.open(url)
    elif args.action == "receipt":
        value = lab.receipt(args.job_id)
        if value is None:
            print(f"labbook: no job {args.job_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(value, indent=2, default=str))
        else:
            print_receipt(value)
    elif args.action == "runs":
        for run in lab.runs():
            print(f"{run['id']:50} {run['turns']:>3} turns  {run['state']:8} {run['status'] or '':9} {run['project']}")
    elif args.action == "replay":
        timeline = lab.timeline(args.run_id)
        if timeline is None:
            print(f"labbook: no run {args.run_id}", file=sys.stderr)
            return 1
        import datetime as dt
        for event in timeline["events"]:
            stamp = dt.datetime.fromtimestamp(event["at"]).strftime("%m-%d %H:%M")
            text = (event.get("text") or "").splitlines()[0][:70] if event.get("text") else ""
            print(f"{stamp}  {event['actor']:7} {event['title']:32} {text}")
        print(json.dumps(timeline["totals"]))
    elif args.action == "public-json":
        from .public import public_snapshot
        print(json.dumps(public_snapshot(lab), indent=2, default=str))
    elif args.action == "config":
        print(f"# {config_path()}")
        print(json.dumps(lab.config, indent=2))
    return 0
