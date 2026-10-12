#!/usr/bin/env python3
"""Transport integration test: the sixteen outbound tools over real HTTP.

Why this exists separately from ``test_extension_whats2api_bridge.py``
------------------------------------------------------------------------
That suite mocks ``httpx``. MockTransport intercepts before a socket
exists, so it cannot catch a wrong method, a header the real client drops,
a URL that does not resolve, a timeout that never fires, or an error
response whose body never gets parsed. Those are exactly the failures that
only appear once a real credential exists -- and the whole reason the
transport half went unexercised for the life of this project.

So: stand up a real HTTP server that speaks the subset of
``api.whats2api.com`` the extension uses, point ``base_url`` at it, and
drive every tool through the real ``McpManager.call_tool`` dispatch with a
real ``httpx.AsyncClient``. Real sockets, real serialization, real
status codes. The only thing stubbed is the provider on the far end.

Run with::

    PYTHONPATH=/Users/maosone/Documents/odysseus \
        python3 -m pytest test_transport_integration.py -q
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.database import init_db
from core.extensions import ExtensionRegistry
from src.mcp_manager import get_mcp_manager


# ── The provider stub ──────────────────────────────────────────────────────

CONNECTIONS = [
    {"_id": "CID-1", "name": "phone", "engine": "baileys",
     "phone": "+5511999999999", "status": "open"},
    {"_id": "CID-2", "name": "tablet", "engine": "whatsmeow",
     "phone": "", "status": "closed"},
]


class _StubState:
    """Shared between the handler and the test."""

    def __init__(self):
        self.requests: list = []
        self.connections = list(CONNECTIONS)
        self.force_status: int | None = None      # next response is this
        self.force_body: str | None = None
        self.hang: bool = False


class _Handler(BaseHTTPRequestHandler):
    state: _StubState = None                      # injected per-test

    def log_message(self, *_a):                   # keep pytest output clean
        pass

    # ── helpers ──
    def _record(self, method: str):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = None
        if raw:
            try:
                body = json.loads(raw.decode())
            except Exception:
                body = raw.decode(errors="replace")
        self.state.requests.append({
            "method": method,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        return body

    def _send(self, status: int, payload, *, raw: str | None = None):
        payload_bytes = (raw if raw is not None
                         else json.dumps(payload)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload_bytes)))
        self.end_headers()
        self.wfile.write(payload_bytes)

    def _dispatch(self, method: str):
        if self.state.hang:
            import time
            time.sleep(5)                        # outlive the client timeout
        body = self._record(method)

        if self.state.force_status is not None:
            status = self.state.force_status
            raw = self.state.force_body
            self.state.force_status = None
            self.state.force_body = None
            self._send(status, {}, raw=raw)
            return

        path = self.path.split("?")[0]
        parts = [p for p in path.split("/") if p]

        # POST /connections/request
        if method == "POST" and path == "/connections/request":
            self._send(200, {"error": False, "result": {
                "messageId": "BAILEYS-ID-1",
                "key": {"id": "BAILEYS-ID-1", "remoteJid": "x@s.whatsapp.net"},
                "echo": body,
            }})
            return

        # /connections...
        if parts[:1] == ["connections"]:
            cid = parts[1] if len(parts) > 1 else None
            action = parts[2] if len(parts) > 2 else None

            if method == "GET" and cid is None:
                self._send(200, self.state.connections)
                return
            if method == "POST" and cid is None:
                new = {"_id": "CID-NEW", "status": "pending",
                       "name": (body or {}).get("name", "")}
                self.state.connections.append(new)
                self._send(200, new)
                return
            if method == "GET":
                for c in self.state.connections:
                    if c["_id"] == cid:
                        self._send(200, c)
                        return
                self._send(404, {"message": "no such connection"})
                return
            if method == "PATCH":
                for c in self.state.connections:
                    if c["_id"] == cid:
                        c.update({k: v for k, v in (body or {}).items()})
                        self._send(200, c)
                        return
                self._send(404, {"message": "no such connection"})
                return
            if method == "DELETE":
                before = len(self.state.connections)
                self.state.connections = [
                    c for c in self.state.connections if c["_id"] != cid]
                self._send(200, {"removed": before - len(self.state.connections)})
                return
            if method == "POST" and action in ("start", "stop"):
                for c in self.state.connections:
                    if c["_id"] == cid:
                        c["status"] = ("open" if action == "start" else "closed")
                        self._send(200, c)
                        return
                self._send(404, {"message": "no such connection"})
                return

        self._send(404, {"message": f"stub has no route for {method} {path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


@pytest.fixture
def stub():
    """A real HTTP server on 127.0.0.1, torn down after the test."""
    state = _StubState()
    handler = type("BoundHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    state.url = f"http://{host}:{port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def bridge(stub):
    """The extension enabled against the stub, through the real registry.

    Settings persist to disk by design (that is how an operator's API token
    survives a restart), which means they also persist *between tests* in a
    session. Left alone, ``test_connection_id_falls_back_to_the_default_setting``
    leaves ``default_connection_id`` behind and every later test inherits it
    — which is how the first draft of this file reported two product bugs
    that did not exist. Reset the settings this file touches.
    """
    init_db()
    registry = ExtensionRegistry()
    registry.rescan()
    running = registry.enable("whats2api_bridge")
    assert running.enabled, running.host.violations
    ns = running.host.get_settings()
    for key in ("base_url", "api_token", "default_connection_id",
                "request_timeout_seconds"):
        try:
            ns.set(key, None)
        except Exception:
            pass
    ns.set("base_url", stub.url)
    ns.set("api_token", "stub-token")
    ns.set("request_timeout_seconds", 30)
    return running


def call(tool: str, args: dict | None = None):
    async def _run():
        return await get_mcp_manager().call_tool(
            f"mcp__oxs_whats2api_bridge__{tool}", args or {},
        )

    return asyncio.run(_run())


# ── Connection lifecycle ───────────────────────────────────────────────────


def test_list_connections_over_real_http(bridge, stub):
    result = call("list_connections")
    assert result["exit_code"] == 0, result
    assert result["output"]["ok"] is True
    assert len(result["output"]["data"]) == 2

    req = stub.requests[-1]
    assert (req["method"], req["path"]) == ("GET", "/connections")
    # The real client must actually authenticate. A mocked transport never
    # sees this, and a missing/renamed auth header is invisible until the
    # provider answers 401 in production.
    assert req["headers"]["authorization"] == "Bearer stub-token"


def test_get_connection(bridge, stub):
    result = call("get_connection", {"connection_id": "CID-1"})
    assert result["exit_code"] == 0, result
    assert result["output"]["data"]["phone"] == "+5511999999999"
    assert stub.requests[-1]["path"] == "/connections/CID-1"


def test_create_connection_posts_the_body(bridge, stub):
    result = call("create_connection", {"name": "phone", "engine": "baileys"})
    assert result["exit_code"] == 0, result
    req = stub.requests[-1]
    assert (req["method"], req["path"]) == ("POST", "/connections")
    assert req["body"] == {"name": "phone", "engine": "baileys"}


def test_patch_connection(bridge, stub):
    result = call("patch_connection",
                  {"connection_id": "CID-1", "name": "renamed"})
    assert result["exit_code"] == 0, result
    req = stub.requests[-1]
    assert (req["method"], req["path"]) == ("PATCH", "/connections/CID-1")
    assert req["body"] == {"name": "renamed"}


def test_start_stop_and_remove(bridge, stub):
    assert call("stop_connection", {"connection_id": "CID-1"})["exit_code"] == 0
    assert (stub.requests[-1]["method"], stub.requests[-1]["path"]) == (
        "POST", "/connections/CID-1/stop")
    assert next(c for c in stub.connections if c["_id"] == "CID-1")["status"] == "closed"

    assert call("start_connection", {"connection_id": "CID-1"})["exit_code"] == 0
    assert next(c for c in stub.connections if c["_id"] == "CID-1")["status"] == "open"

    result = call("remove_connection", {"connection_id": "CID-2"})
    assert result["exit_code"] == 0, result
    assert stub.requests[-1]["method"] == "DELETE"
    assert all(c["_id"] != "CID-2" for c in stub.connections)


def test_refresh_connections_is_an_alias(bridge, stub):
    result = call("refresh_connections")
    assert result["exit_code"] == 0, result
    assert stub.requests[-1]["path"] == "/connections"


def test_health_reports_the_live_count(bridge, stub):
    result = call("health")
    assert result["exit_code"] == 0, result
    data = result["output"]["data"]
    assert data["healthy"] is True
    assert data["connection_count"] == 2


# ── Messaging: every one must reach /connections/request ───────────────────


@pytest.mark.parametrize("tool,args,method,params", [
    ("send_message", {"to": "a@s.whatsapp.net", "text": "hi"},
     "sendMessage", {"to": "a@s.whatsapp.net", "text": "hi"}),
    ("send_reaction", {"key": {"id": "K1"}, "text": "\U0001f600"},
     "sendReaction", {"key": {"id": "K1"}, "text": "\U0001f600"}),
    ("mark_read", {"chat": "c@s.whatsapp.net", "ids": ["m1", "m2"]},
     "readMessages", {"chat": "c@s.whatsapp.net", "ids": ["m1", "m2"]}),
    ("send_media", {"to": "a@s", "kind": "image", "url": "https://x/i.png",
                    "mimetype": "image/png", "caption": "look"},
     "sendMediaMessage", {"to": "a@s", "kind": "image", "url": "https://x/i.png",
                          "mimetype": "image/png", "caption": "look"}),
    ("send_presence", {"presence": "typing"},
     "sendPresenceUpdate", {"presence": "typing"}),
    ("is_on_whatsapp", {"phones": ["+5511999999999"]},
     "isOnWhatsApp", {"phones": ["+5511999999999"]}),
    ("call_method", {"method": "fetchProfile", "params": ["x@s"]},
     "fetchProfile", ["x@s"]),
])
def test_messaging_tool_round_trip(bridge, stub, tool, args, method, params):
    result = call(tool, {**args, "connection_id": "CID-1"})
    assert result["exit_code"] == 0, result
    assert result["output"]["ok"] is True

    req = stub.requests[-1]
    assert (req["method"], req["path"]) == ("POST", "/connections/request")
    assert req["body"] == {
        "connectionId": "CID-1", "method": method, "params": params,
    }
    # The upstream result must survive the envelope intact.
    assert result["output"]["data"]["result"]["messageId"] == "BAILEYS-ID-1"


def test_call_method_defaults_params_to_an_empty_list(bridge, stub):
    result = call("call_method", {"method": "ping", "connection_id": "CID-1"})
    assert result["exit_code"] == 0, result
    assert stub.requests[-1]["body"]["params"] == []


def test_connection_id_falls_back_to_the_default_setting(bridge, stub):
    bridge.host.get_settings().set("default_connection_id", "CID-DEFAULT")
    result = call("send_message", {"to": "a@s", "text": "hi"})
    assert result["exit_code"] == 0, result
    assert stub.requests[-1]["body"]["connectionId"] == "CID-DEFAULT"


# ── Failure modes, over the wire ───────────────────────────────────────────


def test_missing_connection_id_never_reaches_the_wire(bridge, stub):
    result = call("send_message", {"to": "a@s", "text": "hi"})
    assert result["exit_code"] == 1
    assert "connection_id is required" in result["error"]
    assert stub.requests == [], "validation must fail before any HTTP call"


def test_upstream_401_keeps_its_status_and_body(bridge, stub):
    """C-10: the error envelope must not be flattened to a bare string."""
    stub.force_status = 401
    stub.force_body = json.dumps({"code": 401, "message": "jwt malformed"})
    result = call("list_connections")
    assert result["exit_code"] == 1
    assert "HTTP 401" in result["error"]
    assert result.get("status_code") == 401, result
    assert "jwt malformed" in (result.get("body") or ""), result


def test_upstream_500_is_reported_not_swallowed(bridge, stub):
    stub.force_status = 500
    stub.force_body = "upstream exploded"
    result = call("list_connections")
    assert result["exit_code"] == 1
    assert "HTTP 500" in result["error"]
    assert "upstream exploded" in (result.get("body") or "")


def test_unknown_connection_is_a_404_not_a_crash(bridge, stub):
    result = call("get_connection", {"connection_id": "NOPE"})
    assert result["exit_code"] == 1
    assert "404" in result["error"]


def test_provider_unreachable_is_a_friendly_error(bridge, stub):
    """A dead provider must not surface as an unhandled exception."""
    import socket
    with socket.socket() as s:                   # grab a port, then free it
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    bridge.host.get_settings().set("base_url", f"http://127.0.0.1:{dead_port}")
    result = call("list_connections")
    assert result["exit_code"] == 1
    assert "network error" in result["error"].lower(), result
    assert "Traceback" not in result["error"]


def test_health_survives_an_unreachable_provider(bridge, stub):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    bridge.host.get_settings().set("base_url", f"http://127.0.0.1:{dead_port}")
    result = call("health")
    assert result["exit_code"] == 0, "health answers, it does not raise"
    data = result["output"]["data"]
    assert data["healthy"] is False
    assert data["connection_count"] == 0


def test_client_timeout_is_honoured(bridge, stub):
    bridge.host.get_settings().set("request_timeout_seconds", 1)
    stub.hang = True
    result = call("list_connections")
    assert result["exit_code"] == 1
    assert "network error" in result["error"].lower() or "timeout" in str(result).lower()


def test_missing_api_token_fails_before_the_wire(bridge, stub):
    bridge.host.get_settings().set("api_token", "")
    result = call("list_connections")
    assert result["exit_code"] == 1
    assert "api_token is not configured" in result["error"]
    assert stub.requests == []