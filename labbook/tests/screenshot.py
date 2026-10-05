#!/usr/bin/env python3
"""Full-page screenshots through geckodriver, using only the standard library.

    python3 labbook/tests/screenshot.py OUT_DIR URL[#route] [URL...] [--width 1280] [--dark]
"""

import argparse
import base64
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.request


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data, {"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())["value"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--wait", type=float, default=2.5)
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--height", type=int, default=0, help="viewport-only screenshot of this height")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    port = free_port()
    driver = subprocess.Popen(["geckodriver", "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                call(base, "GET", "/status")
                break
            except OSError:
                time.sleep(0.2)
        prefs = {"ui.systemUsesDarkTheme": 1 if args.dark else 0, "layout.css.prefers-color-scheme.content-override": 0 if args.dark else 1}
        session = call(base, "POST", "/session", {"capabilities": {"alwaysMatch": {
            "moz:firefoxOptions": {"args": ["-headless"], "prefs": prefs}}}})["sessionId"]
        try:
            call(base, "POST", f"/session/{session}/window/rect", {"width": args.width, "height": args.height or 1000})
            for index, url in enumerate(args.urls):
                call(base, "POST", f"/session/{session}/url", {"url": url})
                time.sleep(args.wait)
                errors = call(base, "POST", f"/session/{session}/execute/sync", {
                    "script": "return [...document.querySelectorAll('.error')].map(e => e.textContent)", "args": []})
                image = call(base, "GET", f"/session/{session}/screenshot" if args.height else f"/session/{session}/moz/screenshot/full")
                name = out / f"{index:02d}-{url.split('#')[-1].strip('/').replace('/', '_') or 'page'}.png"
                name.write_bytes(base64.b64decode(image))
                print(name, "errors:" if errors else "", *errors)
        finally:
            call(base, "DELETE", f"/session/{session}")
    finally:
        driver.terminate()


if __name__ == "__main__":
    main()
