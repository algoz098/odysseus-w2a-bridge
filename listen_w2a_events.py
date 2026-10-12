#!/usr/bin/env python3
"""listen_w2a_events.py — tail the whats2api_bridge tables in real time.

Watches ``whats2api_bridge__events`` and ``whats2api_bridge__messages``
in the OXS SQLite database and prints each new row as it lands. Designed
for debugging "what is whats2api actually sending us" — every Baileys
sub-event (``Message``, ``RawMessage``, ``IsEphemeral``, ``SourceWebMsg``,
``RetryCount``, ...) shows up here, with the raw payload.

Default DB path is the in-container location
(``/app/data/app.db``). Override with ``--db`` when running on the host
against the bind-mounted file.

Examples
--------
    # Inside the container
    python3 listen_w2a_events.py

    # On the host (bind mount)
    python3 listen_w2a_events.py \\
        --db /Users/me/Documents/odysseus/data/app.db

    # Only Message / messages.upsert fan-outs, full raw JSON
    python3 listen_w2a_events.py --filter Message --raw

    # Dump current state once and exit
    python3 listen_w2a_events.py --from-start --once

    # Show only message-related events and the messages table
    python3 listen_w2a_events.py --only-messages
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from typing import Any, Iterable

DEFAULT_DB_PATHS = (
    "/app/data/app.db",
    "/Users/maosone/Documents/odysseus/data/app.db",
)
DEFAULT_POLL_MS = 500
RAW_PREVIEW_BYTES = 240


def _resolve_db(cli_path: str | None) -> str:
    if cli_path:
        return os.path.expanduser(cli_path)
    for cand in DEFAULT_DB_PATHS:
        if os.path.isfile(cand):
            return cand
    print(
        "ERROR: could not find the OXS DB. Pass --db /path/to/app.db "
        "(or run from inside the container).",
        file=sys.stderr,
    )
    sys.exit(2)


def _fmt_ts(epoch: int | None) -> str:
    if not epoch:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(epoch))) + " UTC"
    except Exception:
        return str(epoch)


def _short(s: str | None, n: int = 12) -> str:
    if not s:
        return "—"
    return s if len(s) <= n else s[: n - 1] + "…"


def _preview_raw(raw: str | None, full: bool) -> str:
    if not raw:
        return ""
    if full:
        try:
            return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            return raw
    if len(raw) <= RAW_PREVIEW_BYTES:
        return raw
    return raw[:RAW_PREVIEW_BYTES] + f"  …[{len(raw) - RAW_PREVIEW_BYTES} more bytes]"


def _print_event(row: sqlite3.Row, full_raw: bool) -> None:
    eid, event_name, conn_id, user_id, received_at, raw_json = row
    print(
        f"[{_fmt_ts(received_at)}] "
        f"EVT  id={eid:<5}  {event_name:<24}  "
        f"conn={_short(conn_id, 14)}  user={_short(user_id, 8)}"
    )
    if raw_json:
        body = _preview_raw(raw_json, full_raw)
        for line in body.splitlines():
            print(f"      {line}")


def _print_message(row: sqlite3.Row, full_raw: bool) -> None:
    (
        mid, conn_id, direction, remote_jid, from_me,
        chat_jid, sender_jid, push_name, text, message_type, ts, raw_json,
    ) = row
    arrow = "←" if direction == "in" else "→" if direction == "out" else "?"
    text_short = (text or "").replace("\n", " ")
    if len(text_short) > 80:
        text_short = text_short[:79] + "…"
    partial = "  [PARTIAL]" if message_type == "partial" else ""
    print(
        f"[{_fmt_ts(ts)}] "
        f"MSG  {arrow} {direction:<3}  "
        f"id={_short(mid, 22)}  type={message_type or '—'}{partial}"
    )
    print(
        f"      jid={_short(remote_jid, 28)}  "
        f"chat={_short(chat_jid, 28)}  "
        f"sender={_short(sender_jid, 20)}  "
        f"push={_short(push_name, 14)}"
    )
    print(f"      text={text_short!r}")
    if full_raw and raw_json:
        body = _preview_raw(raw_json, True)
        for line in body.splitlines():
            print(f"      {line}")


class Tailer:
    """Polls the OXS SQLite DB for new rows in events + messages."""

    def __init__(
        self,
        db_path: str,
        poll_ms: int,
        from_start: bool,
        once: bool,
        full_raw: bool,
        event_filter: set[str] | None,
        only_messages: bool,
    ) -> None:
        self.db_path = db_path
        self.poll_interval = poll_ms / 1000.0
        self.from_start = from_start
        self.once = once
        self.full_raw = full_raw
        self.event_filter = event_filter
        self.only_messages = only_messages
        self._stop = False
        self._conn: sqlite3.Connection | None = None
        self._cur_event_id = 0
        self._cur_message_rowid = 0
        signal.signal(signal.SIGINT, self._on_sigint)
        signal.signal(signal.SIGTERM, self._on_sigint)

    def _on_sigint(self, *_a: Any) -> None:
        self._stop = True

    def _connect(self) -> None:
        uri = f"file:{self.db_path}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        self._conn.row_factory = sqlite3.Row
        if self.from_start:
            self._cur_event_id = 0
            self._cur_message_rowid = 0
        else:
            self._cur_event_id = self._max_event_id() or 0
            self._cur_message_rowid = self._max_message_rowid() or 0
        print(
            f"--- listening on {self.db_path}  "
            f"(events.id > {self._cur_event_id}, messages.rowid > {self._cur_message_rowid}) ---"
        )
        if self.event_filter:
            print(f"--- filter events: {sorted(self.event_filter)} ---")
        if self.only_messages:
            print("--- only_messages: skipping events table ---")
        print("--- Ctrl-C to stop ---")
        sys.stdout.flush()

    def _max_event_id(self) -> int:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM whats2api_bridge__events"
        ).fetchone()
        return int(row[0]) if row else 0

    def _max_message_rowid(self) -> int:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM whats2api_bridge__messages"
        ).fetchone()
        return int(row[0]) if row else 0

    def _poll_events(self) -> Iterable[sqlite3.Row]:
        assert self._conn is not None
        sql = (
            "SELECT id, event_name, connection_id, user_id, received_at, raw_json "
            "FROM whats2api_bridge__events WHERE id > ?"
        )
        params: list = [self._cur_event_id]
        if self.event_filter:
            placeholders = ",".join("?" * len(self.event_filter))
            sql += f" AND event_name IN ({placeholders})"
            params.extend(sorted(self.event_filter))
        sql += " ORDER BY id ASC"
        for row in self._conn.execute(sql, params):
            self._cur_event_id = max(self._cur_event_id, int(row["id"]))
            yield row

    def _poll_messages(self) -> Iterable[sqlite3.Row]:
        """Stream messages with a rowid cursor; update cursor as we go."""
        assert self._conn is not None
        sql = (
            "SELECT rowid AS _rowid, id, connection_id, direction, remote_jid, "
            "from_me, chat_jid, sender_jid, push_name, text, message_type, ts, "
            "raw_json "
            "FROM whats2api_bridge__messages "
            "WHERE rowid > ? ORDER BY rowid ASC"
        )
        cur = self._cur_message_rowid
        for row in self._conn.execute(sql, (cur,)):
            cur = int(row["_rowid"])
            self._cur_message_rowid = cur
            yield row

    def run(self) -> int:
        self._connect()
        assert self._conn is not None
        try:
            while not self._stop:
                if not self.only_messages:
                    rows = list(self._poll_events())
                    for r in rows:
                        _print_event(r, self.full_raw)
                        sys.stdout.flush()
                rows_m = list(self._poll_messages())
                for r in rows_m:
                    _print_message(r, self.full_raw)
                    sys.stdout.flush()
                if self.once:
                    return 0
                # Sleep in small chunks so Ctrl-C feels snappy.
                slept = 0.0
                while slept < self.poll_interval and not self._stop:
                    time.sleep(min(0.1, self.poll_interval - slept))
                    slept += 0.1
        finally:
            try:
                self._conn.close()
            except Exception:
                pass
        return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Tail whats2api_bridge events/messages from the OXS DB.",
    )
    p.add_argument(
        "--db",
        default=None,
        help="Path to app.db (default: auto-detect in-container or bind mount).",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_POLL_MS,
        help=f"Poll interval in ms (default: {DEFAULT_POLL_MS}).",
    )
    p.add_argument(
        "--from-start",
        action="store_true",
        help="Start from the beginning of the table instead of only new rows.",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Dump current state once and exit (no polling).",
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="Pretty-print the full raw_json field of every row.",
    )
    p.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="EVENT_NAME",
        help="Only show events whose event_name matches (repeatable).",
    )
    p.add_argument(
        "--only-messages",
        action="store_true",
        help="Skip the events table; only show persisted messages.",
    )
    args = p.parse_args()
    db_path = _resolve_db(args.db)
    tailer = Tailer(
        db_path=db_path,
        poll_ms=args.interval,
        from_start=args.from_start,
        once=args.once,
        full_raw=args.raw,
        event_filter=set(args.filter) if args.filter else None,
        only_messages=args.only_messages,
    )
    return tailer.run()


if __name__ == "__main__":
    sys.exit(main())
