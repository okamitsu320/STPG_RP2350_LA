"""``stpg-la``: the logic-analyzer tool's own local page (a separate program, port and origin from stpg-view).

    stpg-la [--port N] [--config-dir DIR] [--results-dir DIR] [--no-device-io] [--demo] [--no-browser]

The server listens on 127.0.0.1 only. Every API call needs this process's token (``X-LA-Token`` header; the page
gets it from its URL), the Host header must name the bound address, and state changes need a same-origin request.
It shares nothing with the STPG Runner GUI but files: analyzer bindings in the per-user STPG folder, and the
accessory config that the Runner's External accessory card selects for a run.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sys
import traceback
from urllib.parse import parse_qs, urlparse
import webbrowser

from stpg_runner.artifacts import RunnerError

from stpgview.config_location import ConfigLocationError, resolve_config_dir, start_line

from .service import LaService, dumps

STATIC_DIR = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
JSON_LIMIT = 1 << 20
ROUTES = {  # name -> (service method, needs POST)
    "state": ("state", False), "analyzers": ("analyzers", False), "results": ("results", False),
    "analyzer_save": ("save_analyzer", True), "analyzer_delete": ("delete_analyzer", True),
    "analyzer_identify": ("identify", True), "wiring": ("wiring", True), "bundle": ("open_bundle", True),
    "plan": ("plan", True), "plan_load": ("load_plan", True), "plan_save": ("save_plan", True),
    "config": ("write_config", True), "mock": ("mock", True), "import": ("import_trace", True),
    "capture": ("open_capture", True), "waveform": ("waveform", True), "marker": ("derive_marker", True),
    "ls": ("listing", True), "newfolder": ("new_folder", True), "ports": ("scan_ports", True),
}


class LaServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: LaService, token: str) -> None:
        super().__init__(address, Handler)
        self.service, self.token = service, token


class Handler(BaseHTTPRequestHandler):
    server: LaServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - base class signature
        if os.environ.get("STPG_LA_DEBUG"):
            super().log_message(format, *args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        for key, value in (("Content-Type", content_type), ("Content-Length", str(len(body))),
                           ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                           ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer")):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, dumps({"error": message}), "application/json")

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{self.headers.get('Host', '')}":
            return False
        return self.headers.get("Sec-Fetch-Site") in (None, "same-origin", "none")

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        if not self._host_ok():
            self._error(403, "bad host")
            return
        try:
            if method == "GET" and (parsed.path == "/" or parsed.path.startswith("/static/")):
                name = "index.html" if parsed.path == "/" else parsed.path[len("/static/"):]
                target = (STATIC_DIR / name).resolve()
                if STATIC_DIR.resolve() not in target.parents or not target.is_file():
                    self._error(404, "not found")
                    return
                self._send(200, target.read_bytes(), TYPES.get(target.suffix.lower())
                           or mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                return
            if not parsed.path.startswith("/api/"):
                self._error(404, "not found")
                return
            token = self.headers.get("X-LA-Token") or (parse_qs(parsed.query).get("token") or [""])[0]
            if not secrets.compare_digest(token, self.server.token):
                self._error(401, "missing or wrong token")
                return
            route = ROUTES.get(parsed.path[5:].replace("/", "_"))
            if route is None:
                self._error(404, "unknown api")
                return
            name, post = route
            if post != (method == "POST"):
                self._error(405, "POST required" if post else "GET required")
                return
            if post and (not self.headers.get("X-LA-Token") or not self._same_origin()):
                self._error(403, "state changes need the token header and a same-origin request")
                return
            handler = getattr(self.server.service, name)
            if post:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > JSON_LIMIT:
                    raise ValueError("request too large")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("JSON object expected")
                result = handler(body.get("wiring")) if name == "wiring" else handler(body)
            else:
                result = handler()
            self._send(200, dumps(result), "application/json")
        except (RunnerError, ValueError, KeyError, TypeError, FileNotFoundError, FileExistsError) as exc:
            self._error(400, str(exc) or type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - report to the page instead of dropping the connection
            if os.environ.get("STPG_LA_DEBUG"):
                traceback.print_exc()
            self._error(500, f"{type(exc).__name__}: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stpg-la", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=0, help="local port (default: any free port)")
    parser.add_argument("--config-dir", help="settings folder shared with stpg-view (default: STPG_CONFIG_DIR, else "
                        "the per-user folder)")
    parser.add_argument("--results-dir", type=Path, help="where Identify, mock and import results and accessory "
                        "configs go (default: ~/stpg-work/la)")
    parser.add_argument("--no-device-io", action="store_true", help="refuse Identify (the only port access)")
    parser.add_argument("--demo", action="store_true", help="offer a simulated analyzer named 'simulated'")
    parser.add_argument("--no-browser", action="store_true", help="print the URL only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        location = resolve_config_dir(args.config_dir)
    except ConfigLocationError as exc:  # never a silent fall-back to another folder
        print(f"stpg-la: {exc}", file=sys.stderr)
        return 2
    service = LaService(config_dir=location.path, config_location=location,
                        results_dir=(args.results_dir or Path.home() / "stpg-work" / "la").resolve(),
                        device_io=not args.no_device_io, demo=args.demo)
    token = secrets.token_urlsafe(24)
    server = LaServer(("127.0.0.1", args.port), service, token)
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    print(start_line("stpg-la", location), flush=True)
    print(f"stpg-la: results folder: {service.results_dir}", flush=True)
    print(f"stpg-la (logic-analyzer tool) at {url}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
