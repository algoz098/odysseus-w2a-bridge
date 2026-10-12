"""Tests for the bundled ``whats2api_bridge`` OXS extension.

These tests use the real on-disk manifest and plugin; they only mock
out the HTTP layer (httpx) so no whats2api account is needed. They
exercise:

- Manifest schema + file hashes.
- Discovery + enable + disable lifecycle.
- 16 MCP tools registered with the expected qualified names.
- Inbound webhook: messages.upsert persists + fires OXS event;
  connection.update fans out; invalid ``whats2api`` header does NOT
  fire OXS events.
- Rate limiting on the inbound webhook.
- ``send_message``/``call_method`` return friendly errors when no
  connection_id is available.
- ``_extract_message`` handles Baileys Long timestamps.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import pytest

# Make sure the bundled extension is on the discovery path.
# This module lives at the repo root, so one dirname gets to the root; the
# extra one it used to take pointed at ~/Documents and broke every check that
# opens a file under EXT_DIR.
EXT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "extensions",
    "whats2api_bridge",
)

# The two tool families the bridge must register, per SPEC §4.1 / §4.2.
# Kept explicit (rather than a count) so a tool that silently stops
# registering fails by name. That is exactly the failure this suite sat on
# for its whole life: the transport half was never implemented, and the
# tool-list assertion had drifted to noise long enough that nobody read it.
TRANSPORT_TOOLS = [
    "list_connections", "get_connection", "create_connection",
    "patch_connection", "remove_connection", "start_connection",
    "stop_connection", "refresh_connections", "health",
    "send_message", "send_reaction", "mark_read", "send_media",
    "send_presence", "is_on_whatsapp", "call_method",
]
READ_TOOLS = [
    "list_messages", "get_message", "get_analysis_queue",
    "list_events", "list_connections_local",
]
EXPECTED_TABLES = [
    "whats2api_bridge__connections",
    "whats2api_bridge__events",
    "whats2api_bridge__messages",
    "whats2api_bridge__intervals",
]
EXPECTED_VERSION = "0.5.2"


from core.database import SessionLocal, engine, init_db
from core.extensions import ExtensionRegistry, webhooks as oxs_webhooks
from core.extensions.events import _subscribers as _event_subscribers
from core.extensions.storage import PluginStorage
from src.mcp_manager import get_mcp_manager


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset OXS singletons between tests so state doesn't leak."""
    oxs_webhooks.reset_for_tests()
    oxs_webhooks.reset_rate_limits()
    _event_subscribers.clear()
    init_db()
    yield
    oxs_webhooks.reset_for_tests()
    oxs_webhooks.reset_rate_limits()
    _event_subscribers.clear()


@pytest.fixture
def registry() -> ExtensionRegistry:
    reg = ExtensionRegistry()
    reg.rescan()
    return reg


# ── Manifest ────────────────────────────────────────────────────────────────


def test_manifest_validates():
    from core.extensions.manifest_schema import validate_manifest
    with open(os.path.join(EXT_DIR, "extension.json")) as f:
        m = validate_manifest(f.read())
    assert m["id"] == "whats2api_bridge"
    # Pinned to the manifest, not to a literal. This suite used to assert
    # "0.1.0" and went red on every version bump — which trains the reader
    # to ignore it, and is how the missing transport tools (§ below) stayed
    # invisible: a suite whose failures are known noise stops being read.
    assert m["version"] == EXPECTED_VERSION, (
        "extension.json version drifted from the suite's expectation; bump "
        "EXPECTED_VERSION deliberately, do not just make it green"
    )
    assert m["entry_point"] == "plugin:register"


def test_file_hashes_match():
    from core.extensions.security import sha256_of_file
    with open(os.path.join(EXT_DIR, "extension.json")) as f:
        import json as _json
        m = _json.load(f)
    for rel, h in m["file_hashes"].items():
        assert sha256_of_file(os.path.join(EXT_DIR, rel)) == h


def test_plugin_is_ast_safe():
    from core.extensions.security import ast_parse_safe, find_register_callable
    with open(os.path.join(EXT_DIR, "plugin.py")) as f:
        tree = ast_parse_safe(f.read())
    assert find_register_callable(tree) is not None


# ── Lifecycle ───────────────────────────────────────────────────────────────


def test_enable_registers_all_capabilities(registry):
    running = registry.enable("whats2api_bridge")
    assert running.enabled
    # 16 transport + 5 local-read MCP tools.
    tool_names = sorted(t.tool_name for t in running.host.mcp_tools)
    assert tool_names == sorted(TRANSPORT_TOOLS + READ_TOOLS), (
        "missing: "
        f"{sorted(set(TRANSPORT_TOOLS + READ_TOOLS) - set(tool_names))}; "
        f"unexpected: {sorted(set(tool_names) - set(TRANSPORT_TOOLS + READ_TOOLS))}"
    )
    # The read half is a brokered private read; the transport half changes
    # WhatsApp state and must stay undeclared so the approval gate fails high.
    read_only = {
        t.tool_name: t.read_only for t in running.host.mcp_tools
    }
    assert all(read_only[n] is True for n in READ_TOOLS), (
        f"read tools must declare read_only=True: "
        f"{[n for n in READ_TOOLS if not read_only[n]]}"
    )
    assert all(read_only[n] is False for n in TRANSPORT_TOOLS), (
        "transport tools must NOT declare read_only — they write to WhatsApp: "
        f"{[n for n in TRANSPORT_TOOLS if read_only[n]]}"
    )
    # 1 inbound webhook, 1 static, 1 panel.
    assert len(running.host.webhooks) == 1
    assert len(running.host.statics) == 1
    assert len(running.host.panels) == 1
    assert running.host.violations == []
    # Pin the auth split rather than a bare route count: the admin/analyzer
    # surface grew from 1 to 16 and the old count assertion had gone stale,
    # but the property that matters is that exactly ONE route is public and
    # it is the read-only status route the static dashboard polls.
    public = sorted(rt.path for rt in running.host.routes if rt.auth == "public")
    assert public == ["/ext-api/whats2api_bridge/status"], (
        f"unexpected public surface: {public}"
    )
    non_admin = sorted(
        rt.path for rt in running.host.routes if rt.auth not in ("admin", "public")
    )
    assert non_admin == [], (
        f"routes that are neither admin nor public: {non_admin}"
    )


def test_disable_unwinds(registry):
    running = registry.enable("whats2api_bridge")
    mgr = get_mcp_manager()
    assert "oxs_whats2api_bridge" in mgr._in_process_tools
    registry.disable("whats2api_bridge")
    assert not registry.get("whats2api_bridge").enabled
    assert "oxs_whats2api_bridge" not in mgr._in_process_tools
    assert oxs_webhooks.list_for_extension("whats2api_bridge") == []


def test_data_tables_created_on_enable(registry):
    registry.enable("whats2api_bridge")
    storage = PluginStorage("whats2api_bridge", engine=engine)
    # ``__intervals`` carries the analyzer's conversation intervals — the
    # harness state that makes "nothing is lost, nothing is analysed twice"
    # possible. It arrived with the analyzer; asserting the exact table set
    # is what keeps the next table from being added without a decision.
    assert sorted(storage.list_own_tables()) == sorted(EXPECTED_TABLES)


# ── Uninstall cleanup ───────────────────────────────────────────────────────


def test_uninstall_drops_data_tables(registry):
    """Uninstall must drop the data tables the extension created (spec
    ``specs/oxs.md:256``). Otherwise uninstalled extensions leak rows that
    no UI can reach, and re-installing later leaves stale data around."""
    running = registry.enable("whats2api_bridge")
    storage = PluginStorage("whats2api_bridge", engine=engine)
    # Persist a row so the drop is observable, not just the table names.
    storage.insert(
        "messages",
        id="KEEP_ME",
        connection_id="C",
        direction="in",
        text="hello",
        ts=1700000000,
    )
    assert storage.list_own_tables()  # non-empty before uninstall

    registry.uninstall("whats2api_bridge")
    # After uninstall the prefix should own no tables.
    assert storage.list_own_tables() == []
    # And re-creating the row would fail (the table is gone).
    with pytest.raises(Exception):
        storage.insert(
            "messages",
            id="POST_UNINSTALL",
            connection_id="C",
            direction="in",
            text="nope",
            ts=1700000001,
        )


def test_uninstall_keeps_admin_owned_mcp_servers(registry):
    """McpServer rows with ``extension_id IS NULL`` belong to the operator;
    uninstalling a non-related extension must NOT touch them. Only rows
    tagged with the extension's id are cleaned up."""
    from core.database import McpServer
    registry.enable("whats2api_bridge")
    with SessionLocal() as db:
        admin_row = McpServer(
            id="admin_owned",
            name="Admin MCP",
            transport="stdio",
            command="some-cli",
            args="[]",
            env="{}",
            is_enabled=True,
            extension_id=None,  # operator-created
        )
        db.add(admin_row)
        db.commit()
    try:
        registry.uninstall("whats2api_bridge")
        with SessionLocal() as db:
            still_there = db.query(McpServer).filter(
                McpServer.id == "admin_owned"
            ).first()
            assert still_there is not None
            assert still_there.extension_id is None
    finally:
        # Tidy the test row regardless of outcome.
        with SessionLocal() as db:
            db.query(McpServer).filter(
                McpServer.id == "admin_owned"
            ).delete()
            db.commit()


def test_uninstall_removes_extension_owned_mcp_server(registry):
    """McpServer rows tagged with the extension's id are removed on
    uninstall, even if the extension's ``on_uninstall`` hook forgets to
    do it. This is the framework's defense-in-depth cleanup."""
    from core.database import McpServer
    registry.enable("whats2api_bridge")
    with SessionLocal() as db:
        # Simulate the extension's row. The real plugin sets
        # extension_id via the admin PATCH; this test bypasses the UI
        # path to keep the test focused on the cleanup contract.
        db.add(McpServer(
            id="whats2api",
            name="Whats2API MCP",
            transport="http",
            args="[]",
            env="{}",
            url="https://api.whats2api.com/mcp/fake",
            is_enabled=True,
            extension_id="whats2api_bridge",
        ))
        db.commit()

    registry.uninstall("whats2api_bridge")
    with SessionLocal() as db:
        rows = (
            db.query(McpServer)
            .filter(McpServer.extension_id == "whats2api_bridge")
            .all()
        )
        assert rows == [], "extension-owned McpServer rows were not removed"


def test_uninstall_continues_on_partial_failure(registry, monkeypatch):
    """If one cleanup step raises (e.g. table drop fails on a locked DB),
    the others still run. The operator sees every failure in the audit
    log; nothing blocks the rest of the uninstall."""
    from core.extensions import migrate as migrate_mod
    from core.extensions import registry as reg_mod

    calls: list[str] = []

    def boom(*a, **kw):
        calls.append("drop_all_for_extension")
        raise RuntimeError("simulated DB lock")

    real_mcp_cleanup = reg_mod.ExtensionRegistry._cleanup_extension_resources

    def tracked_cleanup(self, ext_id, *, operator):
        calls.append("cleanup_start")
        monkeypatch.setattr(migrate_mod, "drop_all_for_extension", boom)
        try:
            real_mcp_cleanup(self, ext_id, operator=operator)
        finally:
            monkeypatch.undo()
        calls.append("cleanup_end")

    monkeypatch.setattr(
        reg_mod.ExtensionRegistry,
        "_cleanup_extension_resources",
        tracked_cleanup,
    )

    # Even with the drop step raising, uninstall must complete and the
    # extension must be gone from the in-memory registry.
    registry.enable("whats2api_bridge")
    registry.uninstall("whats2api_bridge")
    assert "whats2api_bridge" not in registry
    assert "cleanup_start" in calls
    assert "cleanup_end" in calls
    assert "drop_all_for_extension" in calls


def test_uninstall_keeps_data_dir(registry, tmp_path, monkeypatch):
    """Uninstall must NOT remove the on-disk source dir
    (``data/extensions/<id>/``). The current contract is "kept for
    recovery"; wiping it would make a re-install of the same version
    impossible without re-uploading the ZIP. Cleanup is a separate
    explicit operator action."""
    # The bundled extension's data dir is a real path on disk; just
    # assert the dir still exists after uninstall.
    running = registry.enable("whats2api_bridge")
    source_dir = running.source_path
    assert os.path.isdir(source_dir)
    registry.uninstall("whats2api_bridge")
    assert os.path.isdir(source_dir), (
        "uninstall must not delete the source dir; that's a separate action"
    )


def test_admin_summary_includes_capabilities(registry):
    running = registry.enable("whats2api_bridge")
    summary_caps = set(running.host._declared_capabilities.keys())
    assert {"mcp_tools", "webhooks_in", "webhooks_out", "events_fire",
            "events_sub", "static", "ui_panel", "routes"}.issubset(summary_caps)


# ── Inbound webhook ─────────────────────────────────────────────────────────


def test_webhook_messages_upsert_persists_and_fires(registry):
    running = registry.enable("whats2api_bridge")
    storage = PluginStorage("whats2api_bridge", engine=engine)

    # Capture OXS events fired by the extension.
    captured: list[tuple[str, dict]] = []

    def _on_msg(payload):
        captured.append(("whats2api.message.received", dict(payload)))

    from core.extensions.events import wire_subscriptions
    wire_subscriptions("test-listener", [("whats2api.message.received", _on_msg)])
    try:
        records = oxs_webhooks.list_for_extension("whats2api_bridge")
        token = oxs_webhooks.get_raw_token(records[0])

        body = {
            "messages.upsert": {
                "type": "notify",
                "messages": [{
                    "key": {"remoteJid": "5511999999999@s.whatsapp.net",
                            "fromMe": False, "id": "TESTID123"},
                    "pushName": "Test User",
                    "message": {"conversation": "hello"},
                    "messageTimestamp": 17478213000,
                }],
            },
            "connection.update": {"state": "open", "connectionId": "C1"},
        }

        async def fire():
            status, _msg = await oxs_webhooks.dispatch(
                "whats2api_bridge", token,
                json.dumps(body).encode(),
                headers={"whats2api": "true", "userId": "u1", "connectionId": "C1"},
                query={},
            )
            assert status == 202

        asyncio.run(fire())
        # The handler runs in a background asyncio task; let it finish.
        import time
        time.sleep(0.5)

        # The messages table has a row.
        msgs = storage.execute_raw(
            "SELECT id, direction, remote_jid, text, push_name, ts "
            "FROM {tbl}".format(tbl=storage.table_name("messages"))
        )
        assert len(msgs) == 1
        assert msgs[0]["id"] == "TESTID123"
        assert msgs[0]["direction"] == "in"
        assert msgs[0]["remote_jid"] == "5511999999999@s.whatsapp.net"
        assert msgs[0]["text"] == "hello"
        assert msgs[0]["push_name"] == "Test User"

        # The events table has both events persisted.
        events = storage.execute_raw(
            "SELECT event_name, connection_id FROM {tbl} ORDER BY received_at"
            .format(tbl=storage.table_name("events"))
        )
        event_names = sorted(e["event_name"] for e in events)
        assert event_names == sorted(["messages.upsert", "connection.update"])

        # The OXS event fired exactly once for messages.received.
        msg_events = [p for (n, p) in captured if n == "whats2api.message.received"]
        assert len(msg_events) == 1
        assert msg_events[0]["connection_id"] == "C1"
        assert msg_events[0]["message"]["text"] == "hello"
        assert msg_events[0]["message"]["remote_jid"] == "5511999999999@s.whatsapp.net"
    finally:
        from core.extensions.events import unwire_subscriptions
        unwire_subscriptions("test-listener")


def test_webhook_without_whats2api_header_persists_but_does_not_fire(registry):
    """An unverified source should NOT fire OXS events, but events are still logged."""
    running = registry.enable("whats2api_bridge")
    storage = PluginStorage("whats2api_bridge", engine=engine)

    captured: list[dict] = []

    def _on_msg(payload):
        captured.append(dict(payload))

    from core.extensions.events import wire_subscriptions
    wire_subscriptions("test-listener", [("whats2api.message.received", _on_msg)])
    try:
        records = oxs_webhooks.list_for_extension("whats2api_bridge")
        token = oxs_webhooks.get_raw_token(records[0])

        body = {
            "messages.upsert": {
                "type": "notify",
                "messages": [{
                    "key": {"remoteJid": "x", "fromMe": False, "id": "Y"},
                    "message": {"conversation": "spoofed"},
                }],
            },
        }

        async def fire():
            # No 'whats2api: true' header → unverified source.
            status, _msg = await oxs_webhooks.dispatch(
                "whats2api_bridge", token,
                json.dumps(body).encode(),
                headers={"userId": "u", "connectionId": "c"},
                query={},
            )
            assert status == 202

        asyncio.run(fire())
        import time
        time.sleep(0.3)

        # Event is logged to the events table…
        events = storage.execute_raw(
            "SELECT event_name FROM {tbl}".format(tbl=storage.table_name("events"))
        )
        assert any(e["event_name"] == "messages.upsert" for e in events)
        # …but the OXS event did NOT fire.
        assert captured == []
    finally:
        from core.extensions.events import unwire_subscriptions
        unwire_subscriptions("test-listener")


def test_webhook_rate_limits(registry):
    running = registry.enable("whats2api_bridge")
    records = oxs_webhooks.list_for_extension("whats2api_bridge")
    token = oxs_webhooks.get_raw_token(records[0])

    body = {"connection.update": {"state": "open"}}

    async def burst():
        results = []
        for _ in range(15):
            status, _msg = await oxs_webhooks.dispatch(
                "whats2api_bridge", token,
                json.dumps(body).encode(),
                headers={"whats2api": "true"},
                query={},
            )
            results.append(status)
        return results

    results = asyncio.run(burst())
    # First 10 are accepted; remaining 5 are rate-limited (429).
    assert results[:10] == [202] * 10
    assert all(s == 429 for s in results[10:])


def test_webhook_invalid_token_returns_404(registry):
    registry.enable("whats2api_bridge")

    async def fire():
        status, _msg = await oxs_webhooks.dispatch(
            "whats2api_bridge", "not-the-real-token",
            b'{}',
            headers={"whats2api": "true"},
            query={},
        )
        return status

    assert asyncio.run(fire()) == 404


# ── MCP tool happy / sad paths ──────────────────────────────────────────────


def test_send_message_requires_connection_id(registry):
    running = registry.enable("whats2api_bridge")
    mgr = get_mcp_manager()

    async def call():
        # No connection_id, no default_connection_id → friendly error.
        result = await mgr.call_tool(
            "mcp__oxs_whats2api_bridge__send_message",
            {"to": "x@s.whatsapp.net", "text": "hi"},
        )
        return result

    result = asyncio.run(call())
    assert result["exit_code"] == 1
    assert "connection_id is required" in result["error"]


def test_call_method_requires_connection_id(registry):
    registry.enable("whats2api_bridge")
    mgr = get_mcp_manager()

    async def call():
        return await mgr.call_tool(
            "mcp__oxs_whats2api_bridge__call_method",
            {"method": "sendMessage", "params": []},
        )

    result = asyncio.run(call())
    assert result["exit_code"] == 1
    assert "connection_id is required" in result["error"]


def _get_loaded_plugin_module():
    """Return the plugin module the OXS loader actually loaded.

    The bundled loader uses ``spec_from_file_location`` with a unique
    module name (``_oxs_<ext_id>_<module>``) so concurrent extensions
    with the same entry-point name don't collide. That means there are
    two module objects pointing at the same source file: one in
    ``sys.modules`` under ``extensions.whats2api_bridge.plugin`` (the
    normal import) and one under ``_oxs_whats2api_bridge_plugin`` (the
    loaded instance). The MCP handlers' closures reference the
    *loaded* instance, so monkey-patches must too.
    """
    import sys
    for name, mod in list(sys.modules.items()):
        if name.startswith("_oxs_whats2api_bridge_plugin"):
            return mod
    raise RuntimeError("loaded plugin module not found in sys.modules")


def test_send_message_uses_default_connection_id(registry):
    running = registry.enable("whats2api_bridge")
    running.host.get_settings().set("api_token", "fake")
    running.host.get_settings().set("default_connection_id", "DEFAULT_CID")
    mgr = get_mcp_manager()

    w2a_plugin = _get_loaded_plugin_module()

    # Patch the underlying httpx transport so we don't hit the real API.
    async def handler(request: httpx.Request) -> httpx.Response:
        # The connection_id should have been resolved from the default.
        payload = json.loads(request.content)
        assert payload["connectionId"] == "DEFAULT_CID"
        assert payload["method"] == "sendMessage"
        return httpx.Response(200, json={
            "error": False,
            "result": {"key": {"id": "ABC123"}},
        })

    transport = httpx.MockTransport(handler)
    orig = w2a_plugin._build_client

    def patched_build_client(host):
        c = orig(host)
        c._transport = transport
        return c

    w2a_plugin._build_client = patched_build_client
    try:
        async def call():
            return await mgr.call_tool(
                "mcp__oxs_whats2api_bridge__send_message",
                {"to": "x@s.whatsapp.net", "text": "hi"},
            )

        result = asyncio.run(call())
        assert result["exit_code"] == 0
        assert result["output"]["ok"] is True
        assert result["output"]["data"]["result"]["key"]["id"] == "ABC123"
    finally:
        w2a_plugin._build_client = orig


def test_health_endpoint_pings_api(registry):
    running = registry.enable("whats2api_bridge")
    running.host.get_settings().set("api_token", "fake")

    w2a_plugin = _get_loaded_plugin_module()

    captured_url: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_url.append(str(request.url))
        return httpx.Response(200, json=[{"_id": "X"}])

    transport = httpx.MockTransport(handler)
    orig = w2a_plugin._build_client

    def patched(host):
        c = orig(host)
        c._transport = transport
        return c

    w2a_plugin._build_client = patched
    try:
        async def call():
            return await get_mcp_manager().call_tool(
                "mcp__oxs_whats2api_bridge__health", {},
            )

        result = asyncio.run(call())
        assert result["exit_code"] == 0
        assert result["output"]["ok"] is True
        assert result["output"]["data"]["healthy"] is True
        assert result["output"]["data"]["connection_count"] == 1
    finally:
        w2a_plugin._build_client = orig


# ── Transport tools: the outbound half ──────────────────────────────────────
# The bridge is two things: it receives WhatsApp events (the read tools above
# plus the inbound webhook) and it drives WhatsApp (these). The second half
# shipped as a docstring and a SPEC table with no implementation behind it, so
# every one of these tests was red from the first commit and got reclassified
# as "expectations obsoletas". These pin the wire shapes so that cannot
# happen again silently.


def _mock_client(w2a_plugin, handler):
    """Patch the loaded plugin's client builder onto a MockTransport."""
    transport = httpx.MockTransport(handler)
    orig = w2a_plugin._build_client

    def patched(host):
        c = orig(host)
        c._transport = transport
        return c

    w2a_plugin._build_client = patched
    return orig


@pytest.mark.parametrize("tool,args,method,params", [
    ("send_message", {"to": "a@s.whatsapp.net", "text": "hi"},
     "sendMessage", {"to": "a@s.whatsapp.net", "text": "hi"}),
    ("send_message", {"to": "a@s.whatsapp.net", "text": "hi", "mentions": ["b@s"]},
     "sendMessage", {"to": "a@s.whatsapp.net", "text": "hi", "mentions": ["b@s"]}),
    ("send_reaction", {"key": {"id": "K1"}, "text": "\U0001f600"},
     "sendReaction", {"key": {"id": "K1"}, "text": "\U0001f600"}),
    ("mark_read", {"chat": "c@s.whatsapp.net", "ids": ["m1", "m2"]},
     "readMessages", {"chat": "c@s.whatsapp.net", "ids": ["m1", "m2"]}),
    ("send_media", {"to": "a@s.whatsapp.net", "kind": "image",
                    "url": "https://x/i.png", "mimetype": "image/png"},
     "sendMediaMessage", {"to": "a@s.whatsapp.net", "kind": "image",
                          "url": "https://x/i.png", "mimetype": "image/png"}),
    ("send_presence", {"presence": "typing", "to": "a@s.whatsapp.net"},
     "sendPresenceUpdate", {"presence": "typing", "to": "a@s.whatsapp.net"}),
    ("is_on_whatsapp", {"phones": ["+5511999999999"]},
     "isOnWhatsApp", {"phones": ["+5511999999999"]}),
    ("call_method", {"method": "someHypermeowRpc", "params": [1, 2]},
     "someHypermeowRpc", [1, 2]),
])
def test_transport_tool_maps_to_rpc(registry, tool, args, method, params):
    """Every messaging tool funnels through POST /connections/request with
    {connectionId, method, params} — the one shape whats2api speaks."""
    running = registry.enable("whats2api_bridge")
    running.host.get_settings().set("api_token", "fake")
    w2a_plugin = _get_loaded_plugin_module()
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"error": False, "result": {"ok": 1}})

    orig = _mock_client(w2a_plugin, handler)
    try:
        async def call():
            return await get_mcp_manager().call_tool(
                f"mcp__oxs_whats2api_bridge__{tool}",
                {**args, "connection_id": "CID"},
            )

        result = asyncio.run(call())
        assert result["exit_code"] == 0, result
        assert len(seen) == 1
        method_name, path, payload = seen[0]
        assert (method_name, path) == ("POST", "/connections/request")
        assert payload == {"connectionId": "CID", "method": method, "params": params}
    finally:
        w2a_plugin._build_client = orig


@pytest.mark.parametrize("tool,args", [
    ("send_message", {"text": "hi"}),               # missing to
    ("send_message", {"to": "a@s"}),                 # missing text
    ("send_reaction", {}),                            # missing key
    ("mark_read", {"chat": "c@s"}),                  # missing ids
    ("send_media", {"to": "a@s", "kind": "image"}),  # missing url/mimetype
    ("is_on_whatsapp", {}),                           # missing phones
    ("call_method", {}),                              # missing method
])
def test_transport_tool_rejects_incomplete_args_without_network(
    registry, tool, args,
):
    """A malformed call must fail on the argument check, not 401 at the wire.

    Reaching the network here means the validation was skipped, and the
    operator pays for it in upstream rate limit.
    """
    running = registry.enable("whats2api_bridge")
    running.host.get_settings().set("api_token", "fake")
    w2a_plugin = _get_loaded_plugin_module()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not reach the network: {request.url}")

    orig = _mock_client(w2a_plugin, handler)
    try:
        async def call():
            return await get_mcp_manager().call_tool(
                f"mcp__oxs_whats2api_bridge__{tool}",
                {**args, "connection_id": "CID"},
            )

        result = asyncio.run(call())
        assert result["exit_code"] == 1
        assert "ok" not in result
        assert result["error"]
    finally:
        w2a_plugin._build_client = orig


@pytest.mark.parametrize("tool,method,path,expected", [
    ("list_connections", "GET", "/connections", None),
    ("refresh_connections", "GET", "/connections", None),
    ("get_connection", "GET", "/connections/CID", None),
    ("start_connection", "POST", "/connections/CID/start", None),
    ("stop_connection", "POST", "/connections/CID/stop", None),
    ("remove_connection", "DELETE", "/connections/CID", None),
    ("create_connection", "POST", "/connections", {"name": "phone"}),
])
def test_connection_lifecycle_tool_hits_its_route(
    registry, tool, method, path, expected,
):
    running = registry.enable("whats2api_bridge")
    running.host.get_settings().set("api_token", "fake")
    w2a_plugin = _get_loaded_plugin_module()
    seen: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        seen.append((request.method, request.url.path,
                     json.loads(body) if body else None))
        return httpx.Response(200, json={"_id": "CID", "status": "open"})

    orig = _mock_client(w2a_plugin, handler)
    try:
        async def call():
            return await get_mcp_manager().call_tool(
                f"mcp__oxs_whats2api_bridge__{tool}",
                {"connection_id": "CID", "name": "phone"},
            )

        result = asyncio.run(call())
        assert result["exit_code"] == 0, result
        assert len(seen) == 1
        got_method, got_path, got_body = seen[0]
        assert got_method == method
        assert got_path == path
        if expected is not None:
            assert got_body == expected
    finally:
        w2a_plugin._build_client = orig


def test_health_reports_unreachable_without_raising(registry):
    """health must answer, not explode — an unreachable provider IS the answer."""
    running = registry.enable("whats2api_bridge")
    running.host.get_settings().set("api_token", "fake")
    w2a_plugin = _get_loaded_plugin_module()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    orig = _mock_client(w2a_plugin, handler)
    try:
        async def call():
            return await get_mcp_manager().call_tool(
                "mcp__oxs_whats2api_bridge__health", {},
            )

        result = asyncio.run(call())
        assert result["exit_code"] == 0
        assert result["output"]["data"]["healthy"] is False
        assert result["output"]["data"]["connection_count"] == 0
    finally:
        w2a_plugin._build_client = orig


def test_send_message_fires_the_message_sent_event(registry):
    """SPEC §5.3: a successful send fires ``whats2api.message.sent``.

    Downstream extensions subscribe to this. A send that silently skips the
    event looks identical to a send that worked, from their side.
    """
    from core.extensions.events import _subscribers as subscribers

    running = registry.enable("whats2api_bridge")
    running.host.get_settings().set("api_token", "fake")
    w2a_plugin = _get_loaded_plugin_module()
    fired: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": False, "result": {"key": {"id": "K"}}})

    orig = _mock_client(w2a_plugin, handler)
    try:
        # Subscribers are (extension_id, handler) pairs.
        subscribers["whats2api.message.sent"] = [
            ("whats2api_bridge", lambda payload: fired.append(payload))
        ]
        async def call():
            return await get_mcp_manager().call_tool(
                "mcp__oxs_whats2api_bridge__send_message",
                {"to": "a@s.whatsapp.net", "text": "hi", "connection_id": "CID"},
            )

        result = asyncio.run(call())
        assert result["exit_code"] == 0, result
        assert len(fired) == 1, fired
        payload = fired[0]
        assert payload["connection_id"] == "CID"
        assert payload["to"] == "a@s.whatsapp.net"
        assert payload["text"] == "hi"
    finally:
        subscribers.pop("whats2api.message.sent", None)
        w2a_plugin._build_client = orig


# ── Invariant 1: the analyzer never writes to WhatsApp ──────────────────────


def test_analyzer_is_refused_every_transport_tool(registry):
    """§1's first non-goal, enforced.

    The crew ``enabled_tools`` allowlist inverts over
    ``BUILTIN_TOOL_DESCRIPTIONS``, which holds no ``mcp__*`` names — so the
    scheduler force-includes allowlisted MCP tools but never *excludes* the
    rest. The analyzer's read-only-over-WhatsApp rule therefore cannot be
    expressed as an allowlist, and the prompt saying "NEVER call
    send_message" is not enforcement. Each transport handler checks.
    """
    from core.database import ScheduledTask as _ST, SessionLocal as _SL
    from core.database import Session as _Session
    from src.agent_runtime.authority import RequestAuthority, bind_request_authority

    running = registry.enable("whats2api_bridge")
    running.host.get_settings().set("api_token", "fake")
    w2a_plugin = _get_loaded_plugin_module()

    # Give the analyzer task a session, the way the scheduler does.
    session_id = "analyzer-session-0001"
    db = _SL()
    try:
        db.add(_Session(id=session_id, name="[Task] Conversation Analyzer",
                         endpoint_url="", model="test-model"))
        db.add(_ST(id="task-analyzer-0001",
                   name="[whats2api_bridge] Conversation Analyzer",
                   session_id=session_id, status="active"))
        db.commit()
    finally:
        db.close()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"analyzer must never reach WhatsApp: {request.url}")

    orig = _mock_client(w2a_plugin, handler)
    try:
        async def call(tool):
            authority = RequestAuthority.empty(
                owner=None, session_id=session_id, workspace="",
            )
            with bind_request_authority(authority):
                return await get_mcp_manager().call_tool(
                    f"mcp__oxs_whats2api_bridge__{tool}",
                    {
                        "to": "a@s.whatsapp.net", "text": "hi",
                        "chat": "c@s", "ids": ["m1"],
                        "key": {"id": "K"}, "phones": ["+55"],
                        "method": "sendMessage",
                        "connection_id": "CID", "name": "n",
                    },
                )

        for tool in TRANSPORT_TOOLS:
            result = asyncio.run(call(tool))
            assert result["exit_code"] == 1, (tool, result)
            assert "read-only" in result["error"], (tool, result)
            # The marker is a sibling of "error", not part of the message:
            # ``_format_in_process_result`` forwards the envelope's extra
            # keys so a refusal is machine-classifiable.
            assert result.get("blocked_by") == "analyzer_read_only_policy", (tool, result)
    finally:
        w2a_plugin._build_client = orig


def test_analyzer_can_still_use_the_read_tools(registry):
    """The guard must not be a blanket block — the analyzer's whole job is
    reading intervals and the run dies without these."""
    from src.agent_runtime.authority import RequestAuthority, bind_request_authority

    running = registry.enable("whats2api_bridge")
    running.host.get_settings().set("api_token", "fake")
    w2a_plugin = _get_loaded_plugin_module()

    async def call():
        authority = RequestAuthority.empty(
            owner=None, session_id="someone-elses-session", workspace="",
        )
        with bind_request_authority(authority):
            return await get_mcp_manager().call_tool(
                "mcp__oxs_whats2api_bridge__get_analysis_queue", {},
            )

    result = asyncio.run(call())
    assert result["exit_code"] == 0, result
    assert result["output"]["ok"] is True


def test_guard_fails_closed_when_the_session_lookup_errors(registry):
    """Unreadable is not "nobody".

    An empty answer means "no session is the analyzer", i.e. allow every
    WhatsApp write. If the lookup errors we must refuse instead — the
    analyzer's own runs never use these tools, so a false refusal only
    costs a manual retry.
    """
    from src.agent_runtime.authority import RequestAuthority, bind_request_authority

    running = registry.enable("whats2api_bridge")
    running.host.get_settings().set("api_token", "fake")
    w2a_plugin = _get_loaded_plugin_module()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not reach the network: {request.url}")

    orig = _mock_client(w2a_plugin, handler)
    import core.database as _cdb
    saved = _cdb.SessionLocal

    def _boom(*_a, **_kw):
        raise RuntimeError("database is down")

    _cdb.SessionLocal = _boom
    try:
        async def call():
            authority = RequestAuthority.empty(
                owner=None, session_id="anyone-at-all", workspace="",
            )
            with bind_request_authority(authority):
                return await get_mcp_manager().call_tool(
                    "mcp__oxs_whats2api_bridge__send_message",
                    {"to": "a@s.whatsapp.net", "text": "hi",
                     "connection_id": "CID"},
                )

        result = asyncio.run(call())
    finally:
        _cdb.SessionLocal = saved
        w2a_plugin._build_client = orig

    assert result["exit_code"] == 1, result
    assert result.get("blocked_by") == "analyzer_lookup_unavailable", result


# ── Helpers ─────────────────────────────────────────────────────────────────


def test_extract_message_handles_baileys_long_timestamp():
    """Baileys emits timestamps as {low, high, unsigned}; we coerce to int."""
    from extensions.whats2api_bridge.plugin import _coerce_int, _extract_message
    assert _coerce_int({"low": 1700000000, "high": 0, "unsigned": True}) == 1700000000
    assert _coerce_int(1234567890) == 1234567890
    assert _coerce_int(None) is None

    payload = {
        "type": "notify",
        "messages": [{
            "key": {"remoteJid": "j@s.whatsapp.net", "fromMe": False, "id": "Z"},
            "pushName": "U",
            "message": {"conversation": "yo"},
            "messageTimestamp": {"low": 1700000000, "high": 0, "unsigned": True},
        }],
    }
    m = _extract_message(payload)
    assert m is not None
    assert m["id"] == "Z"
    assert m["text"] == "yo"
    assert m["ts"] == 1700000000


def test_extract_message_handles_extended_text():
    from extensions.whats2api_bridge.plugin import _extract_message
    payload = {
        "type": "notify",
        "messages": [{
            "key": {"remoteJid": "j@s.whatsapp.net", "fromMe": False, "id": "Z"},
            "message": {
                "extendedTextMessage": {"text": "hi from extended"},
            },
        }],
    }
    m = _extract_message(payload)
    assert m["text"] == "hi from extended"


def test_extract_message_returns_none_for_unknown_shapes():
    from extensions.whats2api_bridge.plugin import _extract_message
    assert _extract_message(None) is None
    assert _extract_message({}) is None
    assert _extract_message({"messages": []}) is None
    assert _extract_message({"messages": "not-a-list"}) is None

# ── User-defined webhook URL token ─────────────────────────────────────────


def test_webhook_url_token_user_can_set_and_url_updates(registry):
    """Operator can define a custom URL token via the admin endpoint.

    After the save:
      - the on-disk file holds the new token,
      - the live ``_webhooks`` record's ``token_hash`` is patched so the
        dispatcher accepts the new token immediately,
      - the next ``/admin/config`` GET returns ``webhook_url`` with the
        new token (no container restart needed),
      - dispatching an event with the OLD token returns 404 and with
        the NEW token returns 202.
    """
    running = registry.enable("whats2api_bridge")
    from core.extensions import webhooks as _oxs_wh
    from extensions.whats2api_bridge.plugin import (
        _admin_config_get,
        _admin_config_post,
        _read_inbound_token,
    )

    # Capture the framework-generated token so we can prove it was
    # replaced (not just shadowed).
    recs = _oxs_wh.list_for_extension("whats2api_bridge")
    old_token = _oxs_wh.get_raw_token(recs[0])
    assert old_token, "framework should have generated a token on enable"

    new_token = "operator-chosen-token-1234567890"
    assert new_token != old_token
    r = _admin_config_post(running.host, None, {"webhook_url_token": new_token})
    assert r["ok"] is True

    # 1. File on disk holds the new token.
    assert _read_inbound_token(running.host) == new_token

    # 2. Live record's token_hash now matches the new token.
    recs = _oxs_wh.list_for_extension("whats2api_bridge")
    assert _oxs_wh.verify_token_hash(new_token, recs[0].token_hash)
    assert _oxs_wh.get_raw_token(recs[0]) == new_token
    # And the OLD token no longer hashes to the same value.
    assert not _oxs_wh.verify_token_hash(old_token, recs[0].token_hash)

    # 3. /admin/config surfaces the new URL.
    cfg = _admin_config_get(running.host, None)
    assert cfg["webhook_url"].endswith("/" + new_token)
    assert cfg["webhook_url_token_set"] is True

    # 4. Dispatcher behavior reflects the change.
    async def dispatch(tok):
        return await _oxs_wh.dispatch(
            "whats2api_bridge", tok,
            b'{}',
            headers={"whats2api": "true"},
            query={},
        )
    new_status, _ = asyncio.run(dispatch(new_token))
    old_status, _ = asyncio.run(dispatch(old_token))
    assert new_status == 202
    assert old_status == 404


def test_webhook_url_token_empty_string_rotates_random(registry):
    """Sending ``webhook_url_token: ""`` rotates to a fresh 32-byte
    random token — useful for the UI's "Generate" button."""
    running = registry.enable("whats2api_bridge")
    from core.extensions import webhooks as _oxs_wh
    from extensions.whats2api_bridge.plugin import (
        _admin_config_post,
        _read_inbound_token,
    )

    recs = _oxs_wh.list_for_extension("whats2api_bridge")
    old_token = _oxs_wh.get_raw_token(recs[0])

    r = _admin_config_post(running.host, None, {"webhook_url_token": ""})
    assert r["ok"] is True

    new_token = _read_inbound_token(running.host)
    assert new_token and new_token != old_token
    # 32 random bytes → base64-url ≈ 43 chars without trailing '='.
    assert len(new_token) >= 40


def test_webhook_url_token_validation_rejects_bad_values(registry):
    """Non-URL-safe chars and oversized tokens are rejected. Anything
    else (including very short strings) is accepted."""
    running = registry.enable("whats2api_bridge")
    from extensions.whats2api_bridge.plugin import _admin_config_post
    # Non-URL-safe chars: must raise.
    for bad in ("abc/def", "abc def", "abc#def", "abc?def"):
        try:
            _admin_config_post(running.host, None, {"webhook_url_token": bad})
        except ValueError as e:
            assert "URL-safe" in str(e)
        else:
            raise AssertionError(f"expected ValueError for token {bad!r}")
    # Too long: must raise (>1024 chars).
    try:
        _admin_config_post(running.host, None, {"webhook_url_token": "a" * 1025})
    except ValueError as e:
        assert "1024" in str(e) or "characters" in str(e)
    else:
        raise AssertionError("expected ValueError for token >1024 chars")
    # Arbitrary short strings: must succeed.
    for ok in ("a", "x", "1", "abc", "token"):
        _admin_config_post(running.host, None, {"webhook_url_token": ok})
