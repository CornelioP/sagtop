"""Stdlib HTTP server: static UI, JSON snapshot/detail, SSE event stream."""

from __future__ import annotations

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .engine import Engine

STATIC = os.path.join(os.path.dirname(__file__), "static")
HEARTBEAT_S = 15.0
mimetypes.add_type("font/woff2", ".woff2")


def make_handler(engine: Engine):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):  # keep the terminal quiet
            pass

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            try:
                if path == "/api/snapshot":
                    return self._json(engine.snapshot())
                if path.startswith("/api/node/"):
                    d = engine.detail(unquote(path[len("/api/node/"):]))
                    return self._json(d) if d else self._json({"error": "not found"}, 404)
                if path == "/api/events":
                    raw = parse_qs(url.query).get("since", ["0"])[0]
                    return self._sse(int(raw) if raw.isdigit() else 0)
                return self._static("index.html" if path == "/" else path.lstrip("/"))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if urlparse(self.path).path != "/api/window":
                return self._json({"error": "not found"}, 404)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                window = json.loads(self.rfile.read(length) or b"{}").get("window")
            except ValueError:
                window = None
            if not engine.set_window(window):
                return self._json({"error": "bad window"}, 400)
            self._json({"window": window})

        def _static(self, rel: str) -> None:
            full = os.path.realpath(os.path.join(STATIC, rel))
            if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            with open(full, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400" if "/vendor/" in full else "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, since: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            while True:
                events, seq = engine.events_since(since, HEARTBEAT_S)
                if events is None:
                    self._send("reset", {"seq": seq})
                    return  # client reloads the snapshot and reconnects
                if events:
                    self._send("batch", events, seq)
                    since = seq
                else:
                    self.wfile.write(b": hb\n\n")
                    self.wfile.flush()

        def _send(self, name: str, data, seq: int | None = None) -> None:
            msg = f"event: {name}\n"
            if seq is not None:
                msg += f"id: {seq}\n"
            msg += f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
            self.wfile.write(msg.encode())
            self.wfile.flush()

    return Handler


def serve(engine: Engine, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(engine))
    httpd.daemon_threads = True
    return httpd
