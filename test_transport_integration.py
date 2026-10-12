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
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.database import init_db
from core.extensions import ExtensionRegistry
from src.mcp_manager import get_mcp_manager


# ── The provider stub ──────────────────────────────────────────────────────

# The real provider is a Feathers app and answers GET /connections with a
# paginated envelope, not a bare array. Reproduced here because assuming a
# bare array silently produced connection_count 0 against the live API.
CONNECTIONS = [
    {"_id": "CID-1", "name": "phone", "engine": "baileys",
     "phone": "+5511999999999", "status": "open"},
    {"_id": "CID-2", "name": "tablet", "engine": "hypermeow",
     "phone": "", "status": "open"},
]


def paginate(rows: list) -> dict:
    return {"total": len(rows), "limit": 10, "skip": 0, "data": rows}


ACCOUNT_EMAIL = "operator@example.com"
ACCOUNT_PASSWORD = "hunter2"


class _StubState:
    """Shared between the handler and the test."""

    def __init__(self):
        self.requests: list = []
        self.connections = list(CONNECTIONS)
        self.force_status: int | None = None      # next response is this
        self.force_body: str | None = None
        self.hang: bool = False
        # whats2api mints a JWT here and rejects anything else on the REST
        # surface with `jwt malformed` -- the exact behaviour that made
        # the MCP token look like a valid credential right up until it did
        # not. Reproduced so the test fails the same way the real API does.
        self.jwt_issued = 0
        self.live_jwts: set = set()
        self.reject_next_jwt = False
        # Applies to every response until cleared, so a retry can be tested
        # against a provider that keeps saying no.
        self.persistent_status: int | None = None


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

        # persistent_status models a provider that keeps refusing *business*
        # calls; the login must still work or the retry path is untestable.
        if self.state.persistent_status is not None and path != "/authentication":
            self._send(self.state.persistent_status, {},
                       raw=self.state.force_body or "{}")
            return

        # POST /authentication -> the daily JWT
        if method == "POST" and path == "/authentication":
            if ((body or {}).get("email") != ACCOUNT_EMAIL
                    or (body or {}).get("password") != ACCOUNT_PASSWORD
                    or (body or {}).get("strategy") != "local"):
                self._send(401, {"name": "NotAuthenticated",
                                 "message": "Invalid login", "code": 401})
                return
            self.state.jwt_issued += 1
            token = f"stub-jwt-{self.state.jwt_issued}"
            self.state.live_jwts.add(token)
            self._send(200, {"accessToken": token, "user": {"email": ACCOUNT_EMAIL}})
            return

        # Everything below the fold is JWT-only.
        auth = self.headers.get("Authorization") or ""
        bearer = auth[7:] if auth.startswith("Bearer ") else ""
        if bearer not in self.state.live_jwts:
            self._send(401, {"name": "NotAuthenticated",
                             "message": "jwt malformed", "code": 401,
                             "className": "not-authenticated",
                             "data": {"name": "JsonWebTokenError"}})
            return
        if self.state.reject_next_jwt:
            self.state.reject_next_jwt = False
            self.state.live_jwts.discard(bearer)

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
                self._send(200, paginate(self.state.connections))
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

    Auth goes through the account credentials, because that is the only
    thing the real REST surface accepts: handing it the MCP token returns
    ``401 jwt malformed``. The MCP-token fallback has its own test below.
    """
    init_db()
    registry = ExtensionRegistry()
    registry.rescan()
    running = registry.enable("whats2api_bridge")
    assert running.enabled, running.host.violations
    ns = running.host.get_settings()
    for key in ("base_url", "api_token", "api_email", "api_password",
                "default_connection_id", "request_timeout_seconds"):
        try:
            ns.set(key, None)
        except Exception:
            pass
    ns.set("base_url", stub.url)
    ns.set("api_email", ACCOUNT_EMAIL)
    ns.set("api_password", ACCOUNT_PASSWORD)
    ns.set("request_timeout_seconds", 30)
    _reset_jwt_cache()
    return running


def _reset_jwt_cache():
    """The JWT cache is module-global; a stale one points at a dead stub."""
    for name in list(sys.modules):
        mod = sys.modules.get(name)
        if name.startswith("_oxs_whats2api_bridge_plugin") and mod is not None:
            mod._JWT_CACHE.update({"token": "", "at": 0.0})


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
    # The real client must actually authenticate, and with the JWT the
    # provider actually accepts -- not the stored MCP token, which the real
    # API answers with `jwt malformed`. A mocked transport never sees the
    # header at all, so a wrong one here is invisible until production.
    auth = req["headers"]["authorization"]
    assert auth.startswith("Bearer stub-jwt-"), auth
    # ...and it had to mint it first.
    assert any(r["path"] == "/authentication" for r in stub.requests), \
        "the first REST call must exchange the credentials for a JWT"


def test_mcp_token_alone_is_rejected_like_the_real_api(bridge, stub):
    """The MCP token is valid — on the other door.

    whats2api issues a long-lived MCP token for /mcp/<token> and a daily
    JWT for the REST surface. Handing the REST surface the MCP token gets
    ``401 jwt malformed``, which is exactly what happened with a real
    credential. Pinned so the fallback path can never look like it works.
    """
    ns = bridge.host.get_settings()
    ns.set("api_email", None)
    ns.set("api_password", None)
    ns.set("api_token", "an-mcp-token")
    _reset_jwt_cache()

    result = call("list_connections")
    assert result["exit_code"] == 1
    assert "401" in result["error"], result
    # The provider's own words must survive to the caller.
    assert "jwt malformed" in (result.get("body") or ""), result
    # ...and nothing may have been minted on the way.
    assert not any(r["path"] == "/authentication" for r in stub.requests)


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


# `params` is a POSITIONAL ARRAY the provider validates per engine; an
# object returns `"/params" must be array`. These expectations are the
# contract learned from the live API, not guesses.
@pytest.mark.parametrize("tool,args,method,params", [
    ("send_message", {"to": "a@s.whatsapp.net", "text": "hi"},
     "sendMessage", ["a@s.whatsapp.net", {"text": "hi"}]),
    ("send_reaction", {"key": {"id": "K1", "remoteJid": "c@s"},
                       "text": "\U0001f600"},
     "sendMessage", ["c@s", {"react": {"text": "\U0001f600",
                                       "key": {"id": "K1",
                                               "remoteJid": "c@s"}}}]),
    ("mark_read", {"chat": "c@s.whatsapp.net", "ids": ["m1", "m2"]},
     "readMessages", ["c@s.whatsapp.net", ["m1", "m2"]]),
    ("send_media", {"to": "a@s", "kind": "image", "url": "https://x/i.png",
                    "mimetype": "image/png", "caption": "look"},
     "sendMessage", ["a@s", {"image": {"url": "https://x/i.png",
                                       "mimetype": "image/png",
                                       "caption": "look"}}]),
    ("send_presence", {"presence": "typing"},
     "sendPresenceUpdate", ["typing"]),
    ("is_on_whatsapp", {"phones": ["+5511999999999", "+5511888888888"]},
     "onWhatsApp", ["+5511999999999", "+5511888888888"]),
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
    assert isinstance(req["body"]["params"], list), "params must be an array"
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
    """C-10: the error envelope must not be flattened to a bare string.

    The stub refuses persistently, so this also covers the retry: the client
    re-mints once, still gets refused, and then the caller's error must
    carry the provider's own status and body rather than an empty string.
    """
    call("list_connections")                 # prime a valid JWT first
    stub.persistent_status = 401
    stub.force_body = json.dumps({"code": 401, "message": "jwt malformed"})
    try:
        result = call("list_connections")
    finally:
        stub.persistent_status = None
    assert result["exit_code"] == 1
    assert "HTTP 401" in result["error"]
    assert result.get("status_code") == 401, result
    assert "jwt malformed" in (result.get("body") or ""), result
    # One retry, not a loop.
    assert sum(1 for r in stub.requests if r["path"] == "/authentication") == 2, \
        "expected exactly one re-mint after the first rejection"


def test_upstream_500_is_reported_not_swallowed(bridge, stub):
    call("list_connections")                 # prime a valid JWT first
    stub.force_status = 500
    stub.force_body = "upstream exploded"
    result = call("list_connections")
    assert result["exit_code"] == 1
    assert "HTTP 500" in result["error"]
    assert "upstream exploded" in (result.get("body") or "")
    # A 500 is not an auth failure, so it must not burn a re-mint.
    assert sum(1 for r in stub.requests if r["path"] == "/authentication") == 1


def test_login_failure_is_reported_with_the_providers_reason(bridge, stub):
    """A bad password must say so, not surface as a downstream 401."""
    bridge.host.get_settings().set("api_password", "wrong-password")
    _reset_jwt_cache()
    result = call("list_connections")
    assert result["exit_code"] == 1
    assert "rejected the login" in result["error"], result
    assert "401" in result["error"], result


def test_no_credential_at_all_fails_before_the_wire(bridge, stub):
    ns = bridge.host.get_settings()
    ns.set("api_email", None)
    ns.set("api_password", None)
    ns.set("api_token", "")
    _reset_jwt_cache()
    result = call("list_connections")
    assert result["exit_code"] == 1
    assert "no api credential" in result["error"].lower(), result
    assert stub.requests == [], "no HTTP call may be attempted without a credential"


def test_unknown_connection_is_a_404_not_a_crash(bridge, stub):
    result = call("get_connection", {"connection_id": "NOPE"})
    assert result["exit_code"] == 1
    assert "404" in result["error"]


def _dead_port() -> int:
    """A port nothing is listening on."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_provider_unreachable_is_a_friendly_error(bridge, stub):
    """A dead provider must not surface as an unhandled exception."""
    bridge.host.get_settings().set("base_url", f"http://127.0.0.1:{_dead_port()}")
    result = call("list_connections")
    assert result["exit_code"] == 1
    assert "network error" in result["error"].lower(), result
    assert "Traceback" not in result["error"]


def test_health_survives_an_unreachable_provider(bridge, stub):
    bridge.host.get_settings().set("base_url", f"http://127.0.0.1:{_dead_port()}")
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
    assert ("network error" in result["error"].lower()
            or "timeout" in str(result).lower()), result

# ── hypermeow (whatsmeow) — the engine the live connection actually uses ───
# The method reference is explicit that the two engines do not share names,
# and that hypermeow speaks waE2E protojson. The first version of the
# transport tools hardcoded Baileys spellings, which would have failed at
# the WhatsApp server rather than here.


HYPER = "CID-2"          # engine: hypermeow in the stub


def _last_rpc(stub):
    for req in reversed(stub.requests):
        if req["path"] == "/connections/request":
            return req["body"]
    raise AssertionError("no /connections/request call was made")


def test_engine_is_read_from_the_connection_document(bridge, stub):
    assert call("list_connections", {})["exit_code"] == 0
    result = call("health")
    assert result["exit_code"] == 0, result
    engines = {c["id"]: c["engine"] for c in result["output"]["data"]["connections"]}
    assert engines == {"CID-1": "baileys", "CID-2": "hypermeow"}, engines


def test_list_connections_unwraps_the_paginated_envelope(bridge, stub):
    """whats2api is a Feathers app: GET /connections is {total,limit,skip,data}.

    Assuming a bare list made ``health`` report connection_count 0 and
    ``list_connections`` return an unreadable object, on the live API.
    """
    result = call("list_connections")
    assert result["exit_code"] == 0, result
    data = result["output"]["data"]
    assert isinstance(data, list), data
    assert len(data) == 2


def test_health_counts_through_the_envelope(bridge, stub):
    result = call("health")
    assert result["output"]["data"]["connection_count"] == 2


@pytest.mark.parametrize("tool,args,method", [
    ("send_message", {"to": "a@s", "text": "hi"}, "SendMessage"),
    ("send_reaction", {"key": {"id": "K1", "remoteJid": "c@s"}, "text": "x"},
     "SendMessage"),
    ("mark_read", {"chat": "c@s", "ids": ["m1"]}, "MarkRead"),
    ("send_presence", {"presence": "typing"}, "SendPresence"),
    ("is_on_whatsapp", {"phones": ["+5511999999999"]}, "IsOnWhatsApp"),
])
def test_hypermeow_uses_its_own_method_names(bridge, stub, tool, args, method):
    result = call(tool, {**args, "connection_id": HYPER})
    assert result["exit_code"] == 0, result
    assert _last_rpc(stub)["method"] == method


def test_hypermeow_reaction_is_its_own_message_kind(bridge, stub):
    """protojson: a reaction is a reactionMessage, not a bare {text, key}."""
    call("send_reaction", {"key": {"id": "K1", "remoteJid": "c@s"},
                           "text": "\U0001f600", "connection_id": HYPER})
    params = _last_rpc(stub)["params"]
    assert params == ["c@s", {"reactionMessage": {
        "text": "\U0001f600", "key": {"id": "K1", "remoteJid": "c@s"}}}]


def test_hypermeow_quote_and_mention_live_in_context_info(bridge, stub):
    call("send_message", {"to": "a@s", "text": "hi",
                          "quoted": {"id": "K1"},
                          "mentions": ["b@s"],
                          "connection_id": HYPER})
    jid, message = _last_rpc(stub)["params"]
    assert jid == "a@s"
    # waE2E.Message protojson: a bare {"text": ...} is rejected by the
    # provider with `proto: unknown field "text"`.
    extended = message["extendedTextMessage"]
    assert extended["text"] == "hi"
    assert extended["contextInfo"]["quotedMessage"] == {"id": "K1"}
    assert extended["contextInfo"]["mentionedJid"] == ["b@s"]


def test_hypermeow_media_requires_file_length(bridge, stub):
    """whatsmeow rejects the send without fileLength, and it cannot be guessed."""
    result = call("send_media", {"to": "a@s", "kind": "image",
                                 "url": "https://x/i.png",
                                 "mimetype": "image/png",
                                 "connection_id": HYPER})
    assert result["exit_code"] == 1
    assert "file_length" in result["error"], result


def test_hypermeow_media_nests_under_its_protojson_field(bridge, stub):
    result = call("send_media", {"to": "a@s", "kind": "image",
                                 "url": "https://x/i.png",
                                 "mimetype": "image/png", "caption": "hi",
                                 "file_length": 2048,
                                 "connection_id": HYPER})
    assert result["exit_code"] == 0, result
    jid, message = _last_rpc(stub)["params"]
    assert jid == "a@s"
    assert message["imageMessage"] == {"url": "https://x/i.png",
                                       "mimetype": "image/png",
                                       "fileLength": 2048,
                                       "caption": "hi"}


def test_baileys_and_hypermeow_differ_on_the_same_tool(bridge, stub):
    call("send_message", {"to": "a@s", "text": "hi", "connection_id": "CID-1"})
    baileys = _last_rpc(stub)
    call("send_message", {"to": "a@s", "text": "hi", "connection_id": HYPER})
    hypermeow = _last_rpc(stub)
    assert baileys["method"] == "sendMessage"
    assert hypermeow["method"] == "SendMessage"
