#!/usr/bin/env python3
"""mock_w2a_webhook.py — local HTTP sink to validate whats2api deliveries.

Run this on a free port, point whats2api's webhook URL at it, and watch
what arrives. Use it to disambiguate "is whats2api sending anything?" vs
"is OXS failing to process it?" — if it shows up here but not in
``whats2api_bridge__events``, the bug is on the OXS side; if it doesn't
show up here either, the bug is upstream (whats2api delivery, tunnel,
auth header, etc.).

Examples
--------
    # Just sink — log to stdout, return 200
    python3 mock_w2a_webhook.py --port 7012

    # Sink + forward to the real OXS so events still get persisted
    python3 mock_w2a_webhook.py --port 7012 \\
        --forward http://localhost:7011

    # Persist every request to a JSONL file (one line per request)
    python3 mock_w2a_webhook.py --port 7012 --log /tmp/w2a.jsonl

    # Only accept paths starting with /api/ext/whats2api_bridge/ (default)
    python3 mock_w2a_webhook.py --port 7012 --path-prefix /api/ext/whats2api_bridge/

    # Reject everything with 401 (useful to test whats2api's retry behavior)
    python3 mock_w2a_webhook.py --port 7012 --respond 401

Then point whats2api at::

    https://<your-tunnel>/api/ext/whats2api_bridge/webhook/<anything>

The path token is ignored — the sink accepts whatever whats2api sends.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import sys
import time
import urllib.request
import urllib.error
from typing import Any

# ANSI helpers — keep one place so output stays consistent.
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""
if _USE_COLOR:
    DIM = "\x1b[2m"
    BOLD = "\x1b[1m"
    YEL = "\x1b[33m"
    GRN = "\x1b[32m"
    RED = "\x1b[31m"
    CYN = "\x1b[36m"
    RST = "\x1b[0m"
else:
    DIM = BOLD = YEL = GRN = RED = CYN = RST = ""

INTERESTING_HEADERS = (
    "whats2api",
    "userid",
    "connectionid",
    "x-whats2api-bridge-signature",
    "content-type",
    "content-length",
    "user-agent",
    "x-forwarded-for",
    "host",
)


class WebhookSink(http.server.BaseHTTPRequestHandler):
    """Logs every request; returns ``self.respond_status`` to the caller.

    Class-level state is set on the ``WebhookSink`` class by ``run()``
    before the server starts. Avoids globals in module scope.
    """

    server_version = "mock-w2a-webhook/0.1"

    # Populated by ``run()`` before serve_forever.
    forward_target: str = ""
    respond_status: int = 200
    path_prefix: str = "/"
    jsonl_path: str = ""
    start_time: float = 0.0
    request_count: int = 0

    # ── Quiet down the default access log; we have our own. ────────────
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        return

    # ── Handle every method. ──────────────────────────────────────────
    def do_ANY(self) -> None:  # type: ignore[no-untyped-def]
        self._handle()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_ANY  # type: ignore[assignment]

    def _handle(self) -> None:
        recv_at = time.time()
        n = WebhookSink.request_count = WebhookSink.request_count + 1
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        body_text = raw.decode("utf-8", errors="replace")
        body_json: Any = None
        body_parse_err: str = ""
        if body_text:
            try:
                body_json = json.loads(body_text)
            except Exception as e:
                body_parse_err = f"{type(e).__name__}: {e}"

        pretty_headers = {
            h: self.headers.get(h) for h in INTERESTING_HEADERS if self.headers.get(h) is not None
        }

        matched = self.path.startswith(WebhookSink.path_prefix)
        status = WebhookSink.respond_status if matched else 404
        fwd_status: int | None = None
        fwd_err: str = ""
        if matched and WebhookSink.forward_target:
            fwd_status, fwd_err = self._forward(raw)

        self._log(
            n=n,
            recv_at=recv_at,
            method=self.command,
            path=self.path,
            headers=pretty_headers,
            body_text=body_text,
            body_json=body_json,
            body_parse_err=body_parse_err,
            status=status,
            fwd_status=fwd_status,
            fwd_err=fwd_err,
        )
        self._write_jsonl(
            n=n,
            recv_at=recv_at,
            method=self.command,
            path=self.path,
            headers=pretty_headers,
            body_text=body_text,
            body_json=body_json,
            body_parse_err=body_parse_err,
            status=status,
            fwd_status=fwd_status,
            fwd_err=fwd_err,
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        body = json.dumps({"ok": True, "sink": "mock-w2a-webhook", "n": n}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self, raw: bytes) -> tuple[int | None, str]:
        target = WebhookSink.forward_target.rstrip("/") + self.path
        req = urllib.request.Request(
            target,
            data=raw,
            method=self.command,
            headers={k: v for k, v in self.headers.items()},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, ""
        except urllib.error.HTTPError as e:
            return e.code, f"forward HTTP {e.code}"
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def _log(
        self,
        *,
        n: int,
        recv_at: float,
        method: str,
        path: str,
        headers: dict,
        body_text: str,
        body_json: Any,
        body_parse_err: str,
        status: int,
        fwd_status: int | None,
        fwd_err: str,
    ) -> None:
        elapsed = time.time() - recv_at
        ts = time.strftime("%H:%M:%S", time.localtime(recv_at))
        ms = int(elapsed * 1000)
        status_color = GRN if 200 <= status < 300 else (YEL if status < 500 else RED)
        fwd = ""
        if WebhookSink.forward_target:
            if fwd_status is not None:
                fc = GRN if 200 <= fwd_status < 300 else (YEL if fwd_status < 500 else RED)
                fwd = f" {CYN}→{RST} fwd {fc}{fwd_status}{RST}"
            elif fwd_err:
                fwd = f" {CYN}→{RST} fwd {RED}ERR {fwd_err}{RST}"
        print(
            f"{DIM}{ts}.{int((recv_at % 1) * 1000):03d}{RST} "
            f"#{n:<4} {BOLD}{method:<6}{RST} {path}  "
            f"{status_color}{status}{RST}  {DIM}{ms}ms{RST}{fwd}",
            flush=True,
        )
        for k in ("whats2api", "userid", "connectionid", "x-whats2api-bridge-signature"):
            if k in headers:
                print(f"        {DIM}{k}:{RST} {headers[k]}", flush=True)
        if body_json is not None:
            if isinstance(body_json, dict):
                events = list(body_json.keys())
                if events and all(isinstance(v, (dict, list, type(None))) for v in body_json.values()):
                    print(f"        {DIM}events:{RST} {events}", flush=True)
                    for ev, payload in body_json.items():
                        size = len(json.dumps(payload, default=str))
                        print(f"          {YEL}{ev}{RST}  ({size} bytes)", flush=True)
                else:
                    self._dump_json(body_json)
            else:
                self._dump_json(body_json)
        elif body_text:
            preview = body_text if len(body_text) <= 600 else body_text[:600] + f"…[{len(body_text) - 600} more]"
            print(f"        {DIM}body:{RST} {preview}", flush=True)
            if body_parse_err:
                print(f"        {RED}body parse error: {body_parse_err}{RST}", flush=True)

    def _dump_json(self, obj: Any) -> None:
        text = json.dumps(obj, indent=2, ensure_ascii=False, default=str)
        for line in text.splitlines():
            print(f"        {DIM}│{RST} {line}", flush=True)

    def _write_jsonl(self, **kw: Any) -> None:
        if not WebhookSink.jsonl_path:
            return
        record = {
            "n": kw["n"],
            "ts": kw["recv_at"],
            "method": kw["method"],
            "path": kw["path"],
            "headers": kw["headers"],
            "body": kw["body_json"] if kw["body_json"] is not None else kw["body_text"],
            "body_parse_err": kw["body_parse_err"],
            "status": kw["status"],
            "fwd_status": kw["fwd_status"],
            "fwd_err": kw["fwd_err"],
        }
        try:
            with open(WebhookSink.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            print(f"        {RED}jsonl write failed: {e}{RST}", flush=True)


class ReusableThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    p = argparse.ArgumentParser(
        description="Local HTTP sink to validate whats2api deliveries.",
    )
    p.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0).")
    p.add_argument("--port", type=int, default=7012, help="Bind port (default: 7012).")
    p.add_argument(
        "--path-prefix",
        default="/",
        help="Accept only requests whose path starts with this (default: /).",
    )
    p.add_argument(
        "--forward",
        default="",
        metavar="BASE_URL",
        help="Forward accepted requests to BASE_URL+path (e.g. http://localhost:7011).",
    )
    p.add_argument(
        "--respond",
        type=int,
        default=200,
        help="HTTP status to return on accepted requests (default: 200).",
    )
    p.add_argument(
        "--log",
        default="",
        metavar="PATH",
        help="Append a JSONL line per request to PATH.",
    )
    args = p.parse_args()

    WebhookSink.forward_target = args.forward
    WebhookSink.respond_status = args.respond
    WebhookSink.path_prefix = args.path_prefix
    WebhookSink.jsonl_path = args.log
    WebhookSink.start_time = time.time()

    banner = [
        "",
        f"{BOLD}mock-w2a-webhook{RST} listening on http://{args.host}:{args.port}",
        f"  path prefix  : {args.path_prefix!r}  ({'matches all paths' if args.path_prefix == '/' else '404 outside'})",
        f"  respond      : {args.respond}",
        f"  forward      : {args.forward or '(sink only)'}",
        f"  jsonl log    : {args.log or '(stdout only)'}",
        "",
        f"  point whats2api at:  http://<your-tunnel>:{args.port}{args.path_prefix}<anything>",
        "  Ctrl-C to stop",
        "",
    ]
    print("\n".join(banner), flush=True)

    try:
        with ReusableThreadingServer((args.host, args.port), WebhookSink) as srv:
            srv.serve_forever()
    except KeyboardInterrupt:
        elapsed = time.time() - WebhookSink.start_time
        print(
            f"\n{BOLD}stopped{RST} — served {WebhookSink.request_count} request(s) in {elapsed:.1f}s",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
