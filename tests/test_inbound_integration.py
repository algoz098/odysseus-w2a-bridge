#!/usr/bin/env python3
"""Inbound integration test: real whats2api event shapes, real headers.

The E2E seeds one synthetic ``messages.upsert`` body. That is enough to
prove the happy path, and it is exactly why the rest of the inbound
surface was untested — the provider fans out many more event names and
three envelope shapes, and a parser that only ever sees the seed will
happily pass while dropping every real conversation.

Per SPEC 5.3/5.4, whats2api POSTs a map of ``eventName -> payload`` and
sets three headers on every delivery: ``whats2api: true``, ``userId`` and
``connectionId``. This drives the real dispatcher over real HTTP for each
taxonomy entry and each of the three message shapes.

Run with::

    PYTHONPATH=/Users/maosone/Documents/odysseus \
        python3 tests/test_inbound_integration.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_URL = os.environ.get("ODYSSEUS_E2E_BASE_URL", "http://127.0.0.1:7011")
CONTAINER = os.environ.get("ODYSSEUS_CONTAINER", "odysseus-odysseus-1")
COOKIE = os.environ.get("ODYSSEUS_SESSION_COOKIE", "")

CHAT = "5511900000001@s.whatsapp.net"
SENDER = "5511900000002@s.whatsapp.net"

_failures: list[str] = []
_passes = 0


def _ok(msg: str) -> None:
    global _passes
    _passes += 1
    print(f"  [ok] {msg}", flush=True)


def _fail(msg: str) -> None:
    _failures.append(msg)
    print(f"  [FAIL] {msg}", flush=True)


def _require(cond: bool, msg: str) -> bool:
    if cond:
        _ok(msg)
        return True
    _fail(msg)
    return False


def _token() -> str:
    out = subprocess_run(
        ["docker", "exec", CONTAINER, "cat",
         "/app/data/extensions/whats2api_bridge/inbound_webhook_token"],
    )
    return out.strip()


def subprocess_run(argv):
    import subprocess
    return subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout


RATE_LIMIT_PER_SEC = 10          # SPEC 5.1: 10 req/s/token
_MIN_GAP = 1.0 / RATE_LIMIT_PER_SEC * 2   # headroom for a loaded CI box
_last_post = 0.0


def _post(body: dict, *, headers: dict, token: str, retries: int = 4):
    """POST one delivery, respecting the documented rate limit.

    The first draft of this file fired ~20 requests in a few seconds and
    lost five checks to HTTP 429, which looked exactly like five parser
    bugs: the two last events of the taxonomy silently never arrived, and
    three edge cases got 429 instead of 202. Pace the sends, and treat a
    429 as "too fast", retrying rather than reporting it.
    """
    global _last_post
    import urllib.request
    import urllib.error

    for attempt in range(retries + 1):
        wait = _MIN_GAP - (time.time() - _last_post)
        if wait > 0:
            time.sleep(wait)
        _last_post = time.time()
        req = urllib.request.Request(
            f"{BASE_URL}/api/ext/whats2api_bridge/webhook/{token}",
            data=json.dumps(body).encode(), method="POST",
        )
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(1.0)
                continue
            return e.code
    return 0


REAL_HEADERS = {"whats2api": "true", "userId": "USER-1", "connectionId": "CONN-1"}


def _wait_for_event(name: str, contains: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _event_log_contains(name, contains):
            return True
        time.sleep(0.4)
    return False


def _event_log_contains(name: str, contains: str) -> bool:
    out = subprocess_run(
        ["docker", "exec", CONTAINER, "sh", "-c",
         f"grep -l {contains!r} /app/data/oxs/audit.log 2>/dev/null | head -1"],
    )
    return bool(out.strip())


def _count_events(name: str) -> int:
    out = subprocess_run(
        ["docker", "exec", CONTAINER, "sh", "-c",
         f"grep -c 'extension.event_fired\\|{name}' /app/data/oxs/audit.log "
         "2>/dev/null || echo 0"],
    )
    try:
        return int(out.strip().splitlines()[-1])
    except Exception:
        return 0


def _audit_fanout_count() -> int:
    """How many OXS event fan-outs the audit log has recorded so far."""
    out = subprocess_run(
        ["docker", "exec", CONTAINER, "sh", "-c",
         "grep -c 'whats2api\\.' /app/data/oxs/audit.log 2>/dev/null || echo 0"],
    )
    try:
        return int(out.strip().splitlines()[-1])
    except Exception:
        return 0


def _message_exists(msg_id: str) -> bool:
    import urllib.request
    req = urllib.request.Request(
        f"{BASE_URL}/ext-api/whats2api_bridge/admin/messages?limit=200")
    req.add_header("Cookie", COOKIE)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.loads(r.read().decode() or "{}").get("messages") or []
    except Exception:
        return False
    return any(m.get("id") == msg_id for m in rows)


def main() -> int:
    print("inbound integration (real event shapes + real headers)\n")

    token = _token()
    if not token:
        print("  [FAIL] no pinned webhook token in the container")
        return 1
    _ok(f"pinned webhook token present ({len(token)} chars)")

    now = int(time.time())
    stamp = f"INB{int(now)}"

    # ── 1. The three message shapes ──────────────────────────────────────
    # (1) messages.upsert — full envelope per message.
    up = {
        "messages.upsert": {"type": "notify", "messages": [{
            "key": {"id": f"{stamp}-upsert", "remoteJid": CHAT, "fromMe": False},
            "message": {"conversation": f"shape 1 {stamp}"},
            "pushName": "Upsert Shape",
            "messageTimestamp": now - 7200,
        }]},
    }
    _post(up, headers=REAL_HEADERS, token=token)

    # (2) Message — full envelope, no upsert wrapper.
    full = {
        "Message": {
            "key": {"id": f"{stamp}-full", "remoteJid": CHAT, "fromMe": False},
            "message": {"conversation": f"shape 2 {stamp}"},
            "pushName": "Full Shape",
            "messageTimestamp": now - 7100,
        },
    }
    _post(full, headers=REAL_HEADERS, token=token)

    # (3) Message — bare content, no key, no timestamp. Needs the Info
    #     event ahead of it to be enriched; un-enriched it must still be
    #     persisted with a synthetic id and message_type='partial'.
    bare = {"Message": {"conversation": f"shape 3 {stamp}"}}
    _post(bare, headers=REAL_HEADERS, token=token)

    # ── 2. The rest of the taxonomy ──────────────────────────────────────
    taxonomy = {
        "messages.update": {"type": "notify", "messages": [
            {"key": {"id": f"{stamp}-upd", "remoteJid": CHAT},
             "message": {"conversation": "edited"}}]},
        "messages.delete": {"key": {"id": f"{stamp}-del", "remoteJid": CHAT}},
        "messages.reaction": {"reaction": {"text": "\U0001f600"},
                              "key": {"id": f"{stamp}-upd", "remoteJid": CHAT}},
        "message-receipt.update": {"receipt": {"readAt": now}},
        "connection.update": {"state": "open", "connectionId": "CONN-1"},
        "chats.upsert": [{"id": CHAT, "name": "Contact"}],
        "presence.update": {"presence": "available", "jid": SENDER},
        "blocklist.set": {"jid": SENDER},
        "call": {"from": SENDER, "isVideo": False},
    }
    rejected = [
        name for name, payload in taxonomy.items()
        if _post({name: payload}, headers=REAL_HEADERS, token=token) != 202
    ]
    _require(not rejected,
             f"posted all {len(taxonomy)} SPEC taxonomy events"
             + (f" (rejected: {rejected})" if rejected else ""))

    # ── 3. Header handling ───────────────────────────────────────────────
    _require(_post({"connection.update": {"state": "open"}},
                   headers={"userId": "U", "connectionId": "C"}, token=token) == 202,
             "accepted a delivery with the three headers")

    bad = _post({"connection.update": {"state": "open"}},
                headers={"whats2api": "false"}, token=token)
    _require(bad == 202,
             "a delivery with whats2api:false is still accepted (persisted, "
             "but must not fire OXS events)")
    _require(_post({}, headers=REAL_HEADERS, token=token) == 202,
             "an empty body is accepted without a 5xx")
    _require(_post({"messages.upsert": "not-an-object"},
                   headers=REAL_HEADERS, token=token) == 202,
             "a malformed payload is accepted without a 5xx")

    # ── 3b. An unverified delivery must fan out to nobody ────────────────
    # SPEC 5.4: events fire only when ``whats2api: true`` is present, so a
    # misrouted or spoofed source cannot drive other extensions or trigger
    # task actions. Counting audit entries is the only observable that
    # proves the fan-out did not happen — "the POST returned 202" does not.
    before = _audit_fanout_count()
    _post({"messages.upsert": {"type": "notify", "messages": [{
        "key": {"id": f"{stamp}-spoof", "remoteJid": CHAT},
        "message": {"conversation": "spoofed"},
        "pushName": "Spoof",
        "messageTimestamp": now - 7000,
    }]}},
        headers={"whats2api": "false"}, token=token)
    time.sleep(3)
    _require(_audit_fanout_count() == before,
             "an unverified delivery fires zero OXS events (spoofing blocked)")
    # ...but it must still be persisted, or a legitimate provider with a
    # misconfigured header loses data silently.
    persisted = _message_exists(f"{stamp}-spoof")
    _require(persisted,
             "an unverified delivery is still persisted to the events table")

    # ── 4. Verify what actually landed, through the app's own API ────────
    import urllib.request
    def _api(path: str):
        req = urllib.request.Request(BASE_URL + path)
        req.add_header("Cookie", COOKIE)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")

    time.sleep(3)
    msgs = (_api("/ext-api/whats2api_bridge/admin/messages?limit=200")
            .get("messages") or [])
    by_id = {m.get("id"): m for m in msgs}

    for suffix, label in (("upsert", "messages.upsert envelope"),
                          ("full", "Message full envelope")):
        row = by_id.get(f"{stamp}-{suffix}")
        if _require(row is not None, f"persisted: {label}"):
            _require(bool(row.get("push_name")),
                     f"  push name captured for {label}")
            _require(row.get("chat_jid") == CHAT,
                     f"  chat jid captured for {label}")
            _require(int(row.get("ts") or 0) > 0,
                     f"  timestamp captured for {label}")

    bare_rows = [m for m in msgs if (m.get("text") or "") == f"shape 3 {stamp}"]
    _require(len(bare_rows) == 1,
             "persisted: Message bare content (un-enriched)")
    if bare_rows:
        _require(bool(bare_rows[0].get("id")),
                 "  bare content got a synthetic id (dedupes on replay)")

    events = (_api("/ext-api/whats2api_bridge/admin/events?limit=200")
              .get("events") or [])
    names = {e.get("event_name") for e in events}
    for event_name in taxonomy:
        _require(event_name in names,
                 f"events table recorded {event_name}")
    with_cid = [e for e in events if e.get("connection_id") == "CONN-1"]
    _require(bool(with_cid),
             "connectionId header stored on the event row")

    # ── 5. Cleanup so the analyzer queue stays clean ─────────────────────
    for row in msgs:
        if (row.get("id") or "").startswith(stamp) or \
                (row.get("text") or "") == f"shape 3 {stamp}":
            try:
                _api(f"/ext-api/whats2api_bridge/admin/messages/{row['id']}")
            except Exception:
                pass
            subprocess_run([
                "curl", "-s", "-o", "/dev/null", "-X", "DELETE",
                "-H", f"Cookie: {COOKIE}",
                f"{BASE_URL}/ext-api/whats2api_bridge/admin/messages/{row['id']}",
            ])
    print(f"\n  ({_passes} checks passed"
          + (f", {len(_failures)} failed)" if _failures else ", 0 failed)"))

    if _failures:
        return 1
    print("\nAll inbound-integration checks PASS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())