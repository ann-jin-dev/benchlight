"""Two small HTTP servers from the standard library.

`serve` is the private notebook on 127.0.0.1. It answers only requests whose
Host header names the loopback address, so a web page cannot reach it through
DNS rebinding. `public` is a separate process that can serve nothing but the
public window; it is the only one meant to be exposed (for example through a
tunnel).
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlparse

from .core import Lab
from .public import public_snapshot

WEB = Path(__file__).resolve().parent.parent / "web"
PRIVATE_FILES = {"index.html", "app.js", "app.css", "chart.js", "public.js", "favicon.svg"}
PUBLIC_FILES = {"public.html", "public.js", "app.css", "chart.js", "favicon.svg"}


class Handler(BaseHTTPRequestHandler):
    server_version = "labbook/1"
    files: set[str] = set()
    index = "index.html"

    def log_message(self, format, *args):  # quiet by default; systemd keeps errors
        pass

    def send_json(self, value, status=HTTPStatus.OK, cache="no-store"):
        body = json.dumps(value, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, status=HTTPStatus.OK, kind="text/plain; charset=utf-8"):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                         "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")

    def send_static(self, name):
        if name not in self.files:
            return self.send_text("Not found", HTTPStatus.NOT_FOUND)
        path = WEB / name
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") or
                                                 kind.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if not self.allowed_host():
                return self.send_text("Forbidden host", HTTPStatus.FORBIDDEN)
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self.send_static(self.index)
            if url.path.startswith("/static/"):
                return self.send_static(url.path.removeprefix("/static/"))
            return self.route(url.path, parse_qs(url.query))
        except BrokenPipeError:
            pass
        except Exception as error:  # keep serving; report the failure to the page
            self.send_json({"error": self.describe(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def describe(self, error):
        return f"{type(error).__name__}: {error}"

    def allowed_host(self):
        return True

    def route(self, path, query):
        return self.send_text("Not found", HTTPStatus.NOT_FOUND)


class PrivateHandler(Handler):
    files = PRIVATE_FILES
    lab: Lab

    def allowed_host(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def route(self, path, query):
        lab = self.lab
        if path == "/api/live":
            latest = lab.pulse.latest()
            age = time.time() - latest["timestamp"] if latest and latest.get("timestamp") else None
            return self.send_json({"pulse": latest, "age_seconds": age,
                                   "gpuq_available": lab.gpuq.available(),
                                   "electricity": lab.config["electricity"]})
        if path == "/api/receipts":
            limit = min(5000, int((query.get("limit") or ["300"])[0]))
            project = (query.get("project") or [None])[0]
            return self.send_json({"receipts": lab.receipts(limit=limit, project=project)})
        if match := re.fullmatch(r"/api/receipts/(\d+)", path):
            receipt = lab.receipt(int(match[1]))
            return self.send_json(receipt) if receipt else self.send_json(
                {"error": "No such job"}, HTTPStatus.NOT_FOUND)
        if path == "/api/runs":
            return self.send_json({"runs": lab.runs()})
        if match := re.fullmatch(r"/api/runs/([A-Za-z0-9_.-]+)", path):
            timeline = lab.timeline(match[1])
            return self.send_json(timeline) if timeline else self.send_json(
                {"error": "No such run"}, HTTPStatus.NOT_FOUND)
        if match := re.fullmatch(r"/api/runs/([A-Za-z0-9_.-]+)/turns/(\d+)/(prompt|report)", path):
            text = lab.turn_text(match[1], int(match[2]), match[3])
            return self.send_text(text) if text is not None else self.send_text(
                "Not found", HTTPStatus.NOT_FOUND)
        if path == "/api/energy/days":
            return self.send_json({"days": lab.pulse.days(60)})
        if path == "/api/public-preview":
            return self.send_json(public_snapshot(lab))
        return self.send_text("Not found", HTTPStatus.NOT_FOUND)


class PublicHandler(Handler):
    files = PUBLIC_FILES
    index = "public.html"
    lab: Lab
    cache: tuple[float, dict] = (0.0, {})

    def describe(self, error):
        return "The lab could not build this page right now."  # error text is not allowlisted

    def route(self, path, query):
        if path == "/api/public":
            stamp, value = PublicHandler.cache
            if time.time() - stamp > 5:  # every visitor shares one recent build
                value = public_snapshot(self.lab)
                PublicHandler.cache = (time.time(), value)
            return self.send_json(value, cache="public, max-age=5")
        return self.send_text("Not found", HTTPStatus.NOT_FOUND)


def run(handler: type[Handler], lab: Lab, host: str, port: int):
    handler.lab = lab
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    print(f"labbook listening on http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
