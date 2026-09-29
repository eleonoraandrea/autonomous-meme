"""Loopback-only read-only HTTP dashboard. No mutation endpoints."""

from __future__ import annotations

import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .tui import load_dashboard, load_token_analysis
from .history import load_history, read_rl_events

ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def handler_for(db_path: str):
    class DashboardHandler(BaseHTTPRequestHandler):
        def respond(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            request = urlsplit(self.path)
            path = request.path
            if path == "/api/status":
                try:
                    body = json.dumps(load_dashboard(db_path), allow_nan=False).encode()
                except (sqlite3.Error, ValueError):
                    self.respond(503, json.dumps({"error": "database unavailable"}).encode(), "application/json")
                    return
                self.respond(200, body, "application/json")
            elif path == "/api/decisions":
                try:
                    query = parse_qs(request.query, keep_blank_values=True)
                    if set(query) - {"kind", "page", "anchor"} or any(len(values) != 1 for values in query.values()):
                        raise ValueError("invalid query")
                    kind = query.get("kind", ["all"])[0]
                    page = int(query.get("page", ["1"])[0])
                    anchor = int(query["anchor"][0]) if "anchor" in query else None
                    body = json.dumps(load_history(db_path, kind, page, anchor), allow_nan=False).encode()
                except ValueError:
                    self.respond(400, b'{"error":"invalid history query"}', "application/json")
                    return
                except sqlite3.Error:
                    self.respond(503, b'{"error":"database unavailable"}', "application/json")
                    return
                self.respond(200, body, "application/json")
            elif path == "/api/analysis":
                try:
                    query = parse_qs(request.query, keep_blank_values=True)
                    if set(query) != {"mint"} or len(query["mint"]) != 1:
                        raise ValueError("invalid query")
                    body = json.dumps(load_token_analysis(db_path, query["mint"][0]),
                                      allow_nan=False).encode()
                except ValueError:
                    self.respond(400, b'{"error":"invalid mint"}', "application/json")
                    return
                except sqlite3.Error:
                    self.respond(503, b'{"error":"database unavailable"}', "application/json")
                    return
                self.respond(200, body, "application/json")
            elif path == "/api/events":
                try:
                    query = parse_qs(request.query, keep_blank_values=True)
                    if set(query) - {"since"} or any(len(values) != 1 for values in query.values()):
                        raise ValueError("invalid query")
                    since = int(query.get("since", ["0"])[0])
                    cursor = int(self.headers.get("Last-Event-ID", since))
                    if cursor < 0:
                        raise ValueError("invalid cursor")
                except ValueError:
                    self.respond(400, b'{"error":"invalid event cursor"}', "application/json")
                    return
                if not Path(db_path).is_file():
                    self.respond(503, b'{"error":"database unavailable"}', "application/json")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    self.wfile.write(b"retry: 2000\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                    while True:
                        events, cursor = read_rl_events(db_path, cursor)
                        for event in events:
                            payload = json.dumps(event, separators=(",", ":"), allow_nan=False)
                            self.wfile.write(f"id: {event['id']}\nevent: rl-evaluation\ndata: {payload}\n\n".encode())
                        if events or time.monotonic() - last_heartbeat >= 10:
                            if not events:
                                self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last_heartbeat = time.monotonic()
                        time.sleep(1)
                except (BrokenPipeError, ConnectionResetError, sqlite3.Error, OSError):
                    return
            elif path == "/health":
                self.respond(200, b'{"status":"ok"}', "application/json")
            elif path in ASSETS:
                filename, content_type = ASSETS[path]
                body = files("meme_paper").joinpath("web_assets", filename).read_bytes()
                self.respond(200, body, content_type)
            else:
                self.respond(404, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self):
            self.respond(405, b"Read-only dashboard", "text/plain; charset=utf-8")

    return DashboardHandler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080):
    server = ThreadingHTTPServer((host, port), handler_for(db_path))
    print(f"Paper dashboard at http://{host}:{server.server_port} (read-only)", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
