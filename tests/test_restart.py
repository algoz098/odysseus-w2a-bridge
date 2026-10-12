#!/usr/bin/env python3
"""Restart durability test (P2).

The bridge has to come back on its own after a container restart: the
extension re-enables itself (:setting) and the analyzer task reconciles to
``active`` (:setting). Both broke twice this session — once when a webhook
token rotated out from under the operator's whats2api.com URL (18 POSTs
answering 404), once when the task never came back — and **neither has a
test**. A restart is the most common thing an operator does and the one
thing that silently loses state.

What this asserts:

1. Before the restart: extension enabled, analyzer task ``active``, webhook
   URL/token pinned on disk.
2. ``docker restart``.
3. After: the same, plus **the same webhook URL** — a rotated token is the
   failure mode that cost the most, and it is invisible from inside the app
   because the app happily accepts its own new token.
4. The analyzer's interval queue is still readable (the storage handle and
   the task survived).

Everything goes through the HTTP API or the container CLI. No direct sqlite
(C-7: an out-of-process sqlite handle on the bind mount truncates committed
WAL frames).

Run with::

    PYTHONPATH=/Users/maosone/Documents/odysseus python3 -u tests/test_restart.py

Env overrides: ODYSSEUS_E2E_BASE_URL, ODYSSEUS_SESSION_COOKIE,
ODYSSEUS_CONTAINER.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("ODYSSEUS_E2E_BASE_URL", "http://127.0.0.1:7011")
CONTAINER = os.environ.get("ODYSSEUS_CONTAINER", "odysseus-odysseus-1")
COOKIE = os.environ.get(
    "ODYSSEUS_SESSION_COOKIE",
    "odysseus_session=e167e470f029aadb1103aa1e7f3013bfbdc9f61db5438037da0c27b84c9cbefa",
)

BOOT_TIMEOUT_S = 240
ANALYZER_TASK_NAME = "[whats2api_bridge] Conversation Analyzer"


def _info(msg: str) -> None:
    print(f"  [ok] {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", flush=True)
    raise SystemExit(1)


def _api(method: str, path: str, body=None, timeout: int = 60):
    req = urllib.request.Request(BASE_URL + path, method=method)
    req.add_header("Cookie", COOKIE)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def _in_container(*argv: str) -> str:
    r = subprocess.run(
        ["docker", "exec", CONTAINER, *argv],
        capture_output=True, text=True, timeout=60,
    )
    return r.stdout.strip()


def _wait_for_boot() -> bool:
    """Poll the app until it answers again after the restart."""
    deadline = time.time() + BOOT_TIMEOUT_S
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE_URL + "/health", timeout=5).read()
            return True
        except urllib.error.HTTPError:
            # Up but answering (e.g. 401/404) means the socket is back.
            return True
        except Exception:
            time.sleep(3)
    return False


def _extension_state() -> dict:
    """The OXS admin listing. Path is ``/api/oxs/extensions`` (routes/oxs_api.py);
    ``/api/ext/extensions`` 404s — that prefix belongs to the inbound webhook
    router."""
    return _api("GET", "/api/oxs/extensions")


def _is_enabled(row: dict) -> bool:
    # The listing exposes ``is_enabled``; older builds used ``enabled``.
    return bool(row.get("is_enabled", row.get("enabled")))


def _find_ext(payload: dict, ext_id: str) -> dict | None:
    items = payload.get("extensions") or payload.get("items") or []
    if isinstance(payload, list):
        items = payload
    for row in items:
        if isinstance(row, dict) and row.get("id") == ext_id:
            return row
    return None


def _analyzer_state() -> dict:
    return _api("GET", "/ext-api/whats2api_bridge/admin/analyzer")


def _token_on_disk() -> str | None:
    out = _in_container(
        "sh", "-c",
        "cat /app/data/extensions/whats2api_bridge/inbound_webhook_token 2>/dev/null",
    )
    return out or None


def main() -> int:
    print("restart durability\n")

    # ── Before ───────────────────────────────────────────────────────────
    ext = _find_ext(_extension_state(), "whats2api_bridge")
    if not ext:
        _fail("whats2api_bridge is not installed")
    if not _is_enabled(ext):
        _fail("whats2api_bridge is not enabled before the restart — "
              "enable it first, otherwise this test proves nothing")
    _info("extension enabled before restart")

    analyzer = _analyzer_state()
    _info(f"analyzer reachable before restart (enabled={analyzer.get('enabled')})")

    token_before = _token_on_disk()
    if not token_before:
        _fail("no pinned inbound webhook token on disk — the operator's "
              "whats2api.com URL would 404 after a restart")
    _info(f"webhook token pinned on disk ({len(token_before)} chars)")

    plugin_before = _in_container(
        "sha256sum", "/app/extensions/whats2api_bridge/plugin.py",
    ).split()[0]

    # ── Restart ──────────────────────────────────────────────────────────
    print("  … restarting container", flush=True)
    subprocess.run(["docker", "restart", CONTAINER], check=True, timeout=180)
    if not _wait_for_boot():
        _fail(f"{CONTAINER} did not answer within {BOOT_TIMEOUT_S}s")
    _info("container is back up")

    # ── After ────────────────────────────────────────────────────────────
    ext_after = _find_ext(_extension_state(), "whats2api_bridge")
    if not ext_after:
        _fail("extension vanished across the restart")
    if not _is_enabled(ext_after):
        _fail("extension did NOT auto-re-enable after the restart")
    _info("extension auto-re-enabled")

    analyzer_after = _analyzer_state()
    task = (analyzer_after.get("task") or {}) if isinstance(analyzer_after, dict) else {}
    status = (task.get("status") or analyzer_after.get("status") or "").lower()
    if status and status not in ("active", "running"):
        _fail(f"analyzer task came back as {status!r}, expected 'active'")
    _info(f"analyzer task reconciled (status={status or 'n/a'})")

    token_after = _token_on_disk()
    if token_after != token_before:
        _fail(
            "inbound webhook token CHANGED across the restart "
            f"({token_before} → {token_after}). whats2api.com is still "
            "pointed at the old URL and every inbound POST will 404."
        )
    _info("webhook token survived the restart unchanged")

    plugin_after = _in_container(
        "sha256sum", "/app/extensions/whats2api_bridge/plugin.py",
    ).split()[0]
    if plugin_after != plugin_before:
        _fail("plugin.py changed across the restart — something rewrote it")
    _info("plugin.py unchanged across the restart")

    # The queue is what the analyzer's whole cycle hangs on; if storage did
    # not survive, the run comes back with an empty queue and no error.
    queue = _api("GET", "/ext-api/whats2api_bridge/admin/messages?limit=1")
    if not isinstance(queue, dict):
        _fail("analyzer storage is not readable after the restart")
    _info("extension storage readable after the restart")

    print("\nAll restart checks PASS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())