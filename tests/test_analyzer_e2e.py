"""End-to-end test for the whats2api_bridge Conversation Analyzer.

Everything goes through the live app's HTTP API — no direct sqlite access.
That is a hard requirement, not a style choice: the database lives on the
docker-compose bind mount, and out-of-process sqlite connections used to
checkpoint-truncate the WAL without seeing the app's committed frames
(lost runs, vanished seeds). Keep this test I/O on the API surface.

Cycle proven here::

    seed a uniquely-marked WhatsApp message (via the real webhook ingest)
    -> run the analyzer -> a finding with the marker is persisted
    -> run it again  -> NO duplicate finding (the harness's interval state
                        already sealed the first run's interval)

Run with::

    PYTHONPATH=/Users/maosone/Documents/odysseus python3 -u tests/test_analyzer_e2e.py

Env overrides: ODYSSEUS_E2E_BASE_URL, ODYSSEUS_SESSION_COOKIE.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("ODYSSEUS_E2E_BASE_URL", "http://127.0.0.1:7011")
COOKIE = os.environ.get(
    "ODYSSEUS_SESSION_COOKIE",
    "odysseus_session=e167e470f029aadb1103aa1e7f3013bfbdc9f61db5438037da0c27b84c9cbefa",
)

RUN_TIMEOUT_S = 480
POLL_S = 10
ANALYSIS_ATTEMPTS = 4
MARKER = f"E2E{uuid.uuid4().hex[:10].upper()}"
MSG_ID = f"e2e-{uuid.uuid4().hex[:16]}"
CHAT = "5511000000009@s.whatsapp.net"


def _info(msg: str) -> None:
    print(f"  [ok] {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", flush=True)
    raise SystemExit(1)


def _api(method: str, path: str, body=None, *, raw_body: bytes | None = None,
         content_type: str | None = None, timeout: int = 60):
    req = urllib.request.Request(BASE_URL + path, method=method)
    req.add_header("Cookie", COOKIE)
    data = raw_body
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    elif content_type:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, data, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def _walk_marker(obj, marker: str, out: list) -> None:
    """Collect every dict whose own string fields mention the marker."""
    if isinstance(obj, dict):
        if any(isinstance(v, str) and marker in v for v in obj.values()):
            out.append(obj)
        for v in obj.values():
            _walk_marker(v, marker, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_marker(v, marker, out)


def _marker_findings(marker: str) -> dict:
    counts = {}
    for name, path in (
        ("calendar", "/api/calendar/events"),
        ("notes", "/api/notes"),
        ("tasks", "/api/tasks"),
    ):
        try:
            data = _api("GET", path + "?limit=200")
        except urllib.error.HTTPError:
            counts[name] = 0
            continue
        found: list = []
        _walk_marker(data, marker, found)
        counts[name] = len(found)
    return counts


def _seed_marker_message(marker: str, msg_id: str) -> None:
    tok = _api("GET", "/api/oxs/extensions/whats2api_bridge/webhooks")["webhooks"][-1]["raw_token"]
    now = int(time.time())
    body = {
        "messages.upsert": {
            "type": "notify",
            "messages": [{
                "key": {"id": msg_id, "remoteJid": CHAT, "fromMe": False},
                "message": {
                    "conversation": (
                        f"Prazo firme: entregar o relatório {marker} "
                        f"(código {marker}) até sexta 18h. Não esquecer, "
                        f"é prioridade máxima."
                    ),
                },
                "pushName": "E2E Tester",
                "messageTimestamp": now - 3600,
            }],
        }
    }
    req = urllib.request.Request(
        f"{BASE_URL}/api/ext/whats2api_bridge/webhook/{tok}",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 202):
            _fail(f"webhook seed rejected: HTTP {resp.status}")
    for _ in range(10):
        msgs = _api("GET", "/ext-api/whats2api_bridge/admin/messages?limit=200")
        if any(m.get("id") == msg_id for m in (msgs.get("messages") or [])):
            return
        time.sleep(1)
    _fail("seeded message never became visible through the app API")


def _cleanup(msg_id: str, counts_before: dict) -> None:
    try:
        _api("DELETE", f"/ext-api/whats2api_bridge/admin/messages/{msg_id}")
    except urllib.error.HTTPError:
        pass
    for name, path, key in (
        ("calendar", "/api/calendar/events", "uid"),
        ("notes", "/api/notes", "id"),
        ("tasks", "/api/tasks", "id"),
    ):
        try:
            data = _api("GET", path + "?limit=200")
        except urllib.error.HTTPError:
            continue
        found: list = []
        _walk_marker(data, MARKER, found)
        for obj in found:
            ident = obj.get(key) or obj.get("uid") or obj.get("id")
            if not ident:
                continue
            try:
                _api("DELETE", f"{path}/{ident}")
            except urllib.error.HTTPError:
                pass


def _run_once(label: str) -> dict:
    for attempt in range(3):
        trig = _api("POST", "/ext-api/whats2api_bridge/admin/analyzer/run-now")
        if not trig.get("ok"):
            _fail(f"run-now failed: {trig}")
        run_id = trig["run_id"]
        print(f"  [{label}] run {run_id[:8]} started; polling...", flush=True)
        deadline = time.time() + RUN_TIMEOUT_S
        waited = 0
        while time.time() < deadline:
            detail = _api(
                "GET", f"/ext-api/whats2api_bridge/admin/analyzer/runs/{run_id}"
            )
            run = detail.get("run") or {}
            status = run.get("status")
            if waited and waited % 6 == 0:
                print(f"  [{label}] ...{waited * POLL_S}s status={status}", flush=True)
            waited += 1
            if status not in (None, "running", "queued", "pending"):
                if status == "aborted" and attempt < 2:
                    _info(f"{label}: run {run_id[:8]} aborted (concurrent run?), retrying")
                    break
                return run
            time.sleep(POLL_S)
        else:
            _fail(f"{label}: run {run_id[:8]} timed out after {RUN_TIMEOUT_S}s")
    _fail(f"{label}: could not get a completed run after retries")


def _run_until_analyzed(label: str, attempts: int = ANALYSIS_ATTEMPTS) -> dict:
    """Drive the analyzer until the seeded marker is analyzed.

    The invariant this test must prove is *eventual* analysis with no loss and
    no duplication -- not that any single run consumes the queue. About 1 in
    10 analyzer runs, MiniMax-M3 answers "No new conversation intervals."
    without ever calling `get_analysis_queue`: it answers from the prompt alone
    (measured 7/83 runs over 24h, before and after the harness fixes alike).

    That is safe by construction. A run that never claims an interval cannot
    seal it, so the interval stays queued and the next run picks it up. What it
    does cost is one run of delay and a wrong report line, so the test has to
    retry rather than assert that the very first run does the work.
    """
    last: dict = {}
    for attempt in range(1, attempts + 1):
        last = _run_once(f"{label} {attempt}/{attempts}")
        if last.get("status") != "success":
            _fail(f"{label}: run {attempt} not successful: {last}")
        found = _marker_findings(MARKER)
        if any(found.values()):
            if attempt > 1:
                _info(f"{label}: analyzed on attempt {attempt} (earlier run did not read the queue)")
            return last
        _info(
            f"{label}: attempt {attempt} persisted nothing for the marker; "
            f"interval stays queued, retrying (result: {(last.get('result') or '')[:60]!r})"
        )
    _fail(
        f"{label}: {attempts} runs passed without persisting a finding for the marker "
        f"(last result: {(last.get('result') or '')[:200]!r})"
    )


def main() -> None:
    print(f"E2E marker: {MARKER}", flush=True)

    status = _api("GET", "/ext-api/whats2api_bridge/admin/analyzer")
    if not status.get("ok"):
        _fail(f"analyzer status not ok: {status}")
    settings = status.get("settings") or {}
    if not settings.get("enabled"):
        _fail(f"analyzer not enabled: {settings}")
    _info(f"analyzer reachable and enabled (model={settings.get('model')})")

    _seed_marker_message(MARKER, MSG_ID)
    _info(f"seeded {MSG_ID} via webhook ingest (in-process write)")

    try:
        before = _marker_findings(MARKER)
        if any(before.values()):
            _fail(f"marker findings already exist before the run: {before}")

        run = _run_until_analyzed("run 1")
        result = run.get("result") or ""
        if "paused safely" in result:
            _fail(f"run 1 hit the approval gate: {result[:200]}")
        after = _marker_findings(MARKER)
        if not any(after.values()):
            _fail(f"run 1 succeeded but persisted no finding with the marker (API counts: {after})")
        _info(f"run 1 analysis complete; findings persisted: {after}")

        # The sealed interval must keep a *second* pass from duplicating it.
        run2 = _run_once("run 2")
        if run2.get("status") != "success":
            _fail(f"run 2 not successful: {run2}")
        result2 = run2.get("result") or ""
        if "paused safely" in result2:
            _fail(f"run 2 hit the approval gate: {result2[:200]}")
        after2 = _marker_findings(MARKER)
        if after2 != after:
            _fail(
                f"run 2 duplicated findings: before={after} after={after2} "
                f"(result: {result2[:300]!r})"
            )
        _info(f"run 2 success; no duplicates: {after2}")
    finally:
        _cleanup(MSG_ID, {})
        _info("cleanup done")

    print("E2E PASS", flush=True)


if __name__ == "__main__":
    sys.exit(main())
