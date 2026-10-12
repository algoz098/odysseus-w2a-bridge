#!/usr/bin/env python3
"""Live smoke test against the real whats2api provider.

Everything here goes through the extension's own MCP tools -- the same
``McpManager.call_tool`` path the agent uses -- so it proves the product,
not a parallel implementation of it.

Read-only by default. Sending is opt-in twice over: ``--send`` on the
command line, and it prints the exact text before it goes out.

Usage::

    python3 tests/smoke_live.py                # read-only
    python3 tests/smoke_live.py --send         # also send one message
    python3 tests/smoke_live.py --send --text "custom text"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.expanduser("~/Documents/odysseus"))
sys.path.insert(0, REPO_ROOT)

PASS, FAIL = 0, 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  [ok] {msg}", flush=True)


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {msg}", flush=True)


def build(credentials: dict):
    """An extension instance pointed at the live provider.

    Uses the operator's real settings file (read-only in practice here: the
    only writes are the JWT cache, which lives in memory) but its own
    throwaway database, so the harness cannot touch live rows.
    """
    os.environ.setdefault("DATABASE_URL",
                          "sqlite:///" + tempfile.mktemp(suffix=".db"))
    import conftest  # noqa: F401 -- isolates settings + db for pytest suites

    import src.constants as C
    import shutil
    live = os.path.expanduser(
        "~/Documents/odysseus/data/oxs/permissions/whats2api_bridge.json")
    if os.path.exists(live):
        shutil.copyfile(live, os.path.join(C.OXS_PERMISSIONS_DIR,
                                           "whats2api_bridge.json"))

    from core.database import init_db
    init_db()
    from core.extensions import ExtensionRegistry
    registry = ExtensionRegistry()
    registry.rescan()
    running = registry.enable("whats2api_bridge")
    ns = running.host.get_settings()
    if credentials.get("email"):
        ns.set("api_email", credentials["email"])
    if credentials.get("password"):
        ns.set("api_password", credentials["password"])
    if credentials.get("default_connection_id"):
        ns.set("default_connection_id", credentials["default_connection_id"])
    return running


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true",
                    help="send one real WhatsApp message (outward-facing)")
    ap.add_argument("--text", default=None, help="override the message text")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive confirm (operator already authorised)")
    ap.add_argument("--to", default=None, help="override the destination")
    args = ap.parse_args()

    from load_credentials import read_env
    credentials = read_env()
    target = args.to or credentials.get("WHATS2API_SMOKE_TO")
    text = args.text or (
        f"Teste automatico do Odysseus Whats2API Bridge — {args.send and 'envio real' or 'somente leitura'}"
    )

    print("live smoke against api.whats2api.com\n")
    running = build(credentials)
    if running.host.violations:
        bad(f"extension violations on enable: {running.host.violations}")
        return 1

    import asyncio
    from src.mcp_manager import get_mcp_manager
    mcp = get_mcp_manager()

    def call(tool, arguments=None):
        async def _r():
            return await mcp.call_tool(
                f"mcp__oxs_whats2api_bridge__{tool}", arguments or {})
        return asyncio.run(_r())

    # ── 1. health ───────────────────────────────────────────────────────
    result = call("health")
    data = (result.get("output") or {}).get("data") or {}
    print(f"  health: {json.dumps(data)[:300]}")
    if result["exit_code"] != 0:
        bad(f"health failed: {result}")
        return 1
    if data.get("healthy"):
        ok("provider reachable and authenticated")
    else:
        bad(f"provider unhealthy: status={data.get('status_code')} "
            f"error={data.get('error')}")
        return 1

    # ── 2. connections ──────────────────────────────────────────────────
    connections = data.get("connections") or []
    if not connections:
        bad("no connections on the account")
        return 1
    for c in connections:
        print(f"  connection: id={c.get('id')} name={c.get('name')!r} "
              f"engine={c.get('engine')} status={c.get('status')}")
    ok(f"{len(connections)} connection(s) listed through the paginated envelope")

    live = [c for c in connections if c.get("status") == "open"]
    if not live:
        bad("no connection in status 'open'")
        return 1
    conn = live[0]
    cid = conn["id"]

    # ── 3. the engine actually drives the method name ───────────────────
    print(f"  engine for {cid}: {conn.get('engine')}")
    ok("engine read from the connection document")

    # ── 4. is_on_whatsapp — read-only, proves the RPC round trip ────────
    if target:
        number = target.split("@")[0]
        result = call("is_on_whatsapp", {"phones": [number],
                                         "connection_id": cid})
        print(f"  is_on_whatsapp: {json.dumps(result.get('output') or result)[:300]}")
        if result["exit_code"] == 0:
            ok(f"RPC round trip succeeded for {number}")
        else:
            bad(f"is_on_whatsapp failed: {result}")

    # ── 5. send — opt-in ────────────────────────────────────────────────
    if not args.send:
        print("\n  (not sending; pass --send to send one real message)")
    elif not target:
        bad("--send needs a destination (--to or WHATS2API_SMOKE_TO)")
    else:
        number = target.split("@")[0]
        jid = number if "@" in number else f"{number}@s.whatsapp.net"
        print(f"\n  ABOUT TO SEND over WhatsApp, for real:")
        print(f"    to   : {jid}")
        print(f"    text : {text!r}")
        if args.yes:
            print("    (confirmed by --yes)")
        else:
            answer = input("    type 'yes' to send: ").strip().lower()
            if answer != "yes":
                print("    aborted")
                return 1

        result = call("send_message", {"to": jid, "text": text,
                                       "connection_id": cid})
        print(f"  send_message: {json.dumps(result)[:700]}")
        if result["exit_code"] == 0:
            ok("message sent")
        else:
            bad(f"send_message failed: {result}")

    print(f"\n  {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())