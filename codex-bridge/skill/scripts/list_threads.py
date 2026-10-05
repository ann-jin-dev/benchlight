# Read-only: ask a fresh codex app-server for the thread list the app would see.
import json, subprocess, sys
p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
def call(i, method, params):
    p.stdin.write(json.dumps({"id": i, "method": method, "params": params}) + "\n"); p.stdin.flush()
    for line in p.stdout:
        m = json.loads(line)
        if m.get("id") == i and "method" not in m:
            return m
call(1, "initialize", {"clientInfo": {"name": "probe_readonly", "title": "probe", "version": "0"}})
p.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n"); p.stdin.flush()
# Usage: python3 list_threads.py [project-dir]  -> newest threads overall, and in that project
queries = [{"limit": 15}] + ([{"limit": 15, "cwd": sys.argv[1]}] if len(sys.argv) > 1 else [])
for params in queries:
    r = call(2, "thread/list", params)
    print("== thread/list", params, "error" if "error" in r else "")
    if "error" in r:
        print(r["error"]); continue
    for t in r["result"].get("data", []):
        print(" ", t.get("id"), t.get("name") or (t.get("preview") or "")[:50].replace("\n", " "), t.get("source"), t.get("cwd"), t.get("updatedAt"))
p.stdin.close(); p.wait(timeout=10)
