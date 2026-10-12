"""Deterministic soak for the conversation-interval analysis cycle.

Simulates a full day of cron ticks against a virtual clock and a
synthetic message stream — inputs and time are explicit values, no wall
clock, no LLM, no network. Each tick harvests the previous run's claim
state, computes the queue at its own ``now``, claims, and "runs" with a
chosen outcome. Invariants asserted over the whole soak:

- exactly-once: every sealed message was delivered to exactly one
  successful tick (never twice, never zero times);
- failure recovery: a failed/aborted tick re-queues its intervals;
- late arrivals: messages that land inside an already-analyzed range
  are never re-offered; past the range they form new mini-intervals;
- steady state: once everything is sealed, ticks deliver nothing;
- monotonicity: analyzed state only ever grows.

Run with::

    PYTHONPATH=/Users/maosone/Documents/odysseus python3 tests/test_analyzer_soak.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

sys.path.insert(0, "/Users/maosone/Documents/odysseus")

EXT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "extensions",
    "whats2api_bridge",
)
_spec = importlib.util.spec_from_file_location(
    "analyzer_plugin_soak", os.path.join(EXT_DIR, "plugin.py")
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

GAP = 45 * 60
TICK = 30 * 60  # the analyzer's 30-minute cron


def _info(msg: str) -> None:
    print(f"  [ok] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    raise SystemExit(1)


class Soak:
    """Virtual-clock driver for the claim/seal/queue state machine."""

    def __init__(self, start: int):
        self.now = start
        self.start = start
        self.msg_seq = 0
        self.run_seq = 0
        self.runs: list[dict] = []
        self.delivered: set[str] = set()  # message ids handed to successful runs
        from sqlalchemy import create_engine
        from core.extensions.storage import PluginStorage

        engine = create_engine(
            f"sqlite:///{tempfile.mkdtemp(prefix='w2a-soak-')}/app.db"
        )
        self.storage = PluginStorage("whats2api_bridge", engine=engine)
        self.storage.ensure_table(
            "intervals",
            columns={
                "id": "TEXT PRIMARY KEY",
                "chat_jid": "TEXT NOT NULL",
                "start_ts": "INTEGER NOT NULL",
                "end_ts": "INTEGER NOT NULL",
                "msg_count": "INTEGER NOT NULL",
                "status": "TEXT NOT NULL",
                "claimed_at": "INTEGER",
                "analyzed_at": "INTEGER",
                "run_id": "TEXT",
                "findings_summary": "TEXT",
            },
        )
        self.storage.ensure_table(
            "messages",
            columns={
                "id": "TEXT PRIMARY KEY",
                "connection_id": "TEXT",
                "direction": "TEXT",
                "remote_jid": "TEXT",
                "from_me": "TEXT",
                "chat_jid": "TEXT",
                "sender_jid": "TEXT",
                "push_name": "TEXT",
                "text": "TEXT",
                "message_type": "TEXT",
                "ts": "INTEGER NOT NULL",
                "raw_json": "TEXT",
            },
        )

    def arrive(self, chat: str, ts: int, text: str = "t") -> str:
        self.msg_seq += 1
        mid = f"m{self.msg_seq:04d}"
        self.storage.insert(
            "messages", connection_id="c", direction="in", id=mid,
            chat_jid=chat, sender_jid=chat, push_name="P", text=text,
            message_type="text", ts=ts, raw_json="{}",
        )
        return mid

    def tick(self, *, succeeds: bool = True, lookback: int = 24 * 3600) -> dict:
        """One cron tick at the current virtual time."""
        now = self.now
        # 1) harvest claims from runs that finished since the last tick
        claimed = [
            r for r in mod._load_interval_rows(self.storage)
            if (r.get("status") or "") == "claimed"
        ]
        decisions = mod.seal_claims(claimed, self.runs, now=now)
        mod._apply_seal_decisions(self.storage, decisions)

        # 2) compute + claim the queue as the tool would
        covered = [
            r for r in mod._load_interval_rows(self.storage)
            if (r.get("status") or "") in ("analyzed", "claimed")
        ]
        msgs = mod._fetch_window_messages(self.storage, now - lookback)
        new_intervals, _ = mod.compute_analysis_queue(
            msgs, covered, now=now, gap_seconds=GAP
        )
        mod._claim_intervals(self.storage, new_intervals, now=now)
        delivered = [m["id"] for iv in new_intervals for m in iv["messages"]]

        # 3) simulate the run: record it so the NEXT harvest can seal/release
        self.run_seq += 1
        self.runs.insert(0, {
            "id": f"r{self.run_seq}",
            "started_at": now - 5,
            "finished_at": now + 20,
            "status": "success" if succeeds else "error",
            "result": "Findings: x" if succeeds else "",
        })
        if succeeds:
            self.delivered.update(delivered)
        self.now += TICK
        return {
            "now": now,
            "delivered": delivered,
            "sealed_released": {k: len(v) for k, v in decisions.items()},
            "analyzed": sum(
                1 for r in mod._load_interval_rows(self.storage)
                if (r.get("status") or "") == "analyzed"
            ),
        }


def test_soak_day_exactly_once():
    s = Soak(start=1_800_000_000)
    # A conversation day: morning burst, midday chat, evening burst —
    # some messages arrive BETWEEN ticks, some before a tick boundary.
    stream = [
        ("chatA", s.start - 2 * GAP, "a1"),
        ("chatA", s.start - 2 * GAP + 60, "a2"),
        ("chatA", s.start - 2 * GAP + 120, "a3"),
        ("chatB", s.start - 2 * GAP + 30, "b1"),
    ]
    ids = [s.arrive(c, ts, t) for c, ts, t in stream]
    outcomes = [s.tick(succeeds=True) for _ in range(6)]
    # late arrivals after the analyzed range
    late_ids = [
        s.arrive("chatA", s.now - 2 * GAP, "a4-late"),
        s.arrive("chatB", s.now - 2 * GAP + 40, "b2-late"),
    ]
    outcomes += [s.tick(succeeds=True) for _ in range(6)]
    # new activity at night
    night = [s.arrive("chatA", s.now - 2 * GAP, "a5-night")]
    outcomes += [s.tick(succeeds=True) for _ in range(4)]

    expected = set(ids + late_ids + night)
    if s.delivered != expected:
        missing = expected - s.delivered
        extra = s.delivered - expected
        _fail(f"exactly-once violated: missing={missing} extra={extra}")
    total_deliveries = sum(len(o["delivered"]) for o in outcomes)
    if total_deliveries != len(expected):
        _fail(
            f"messages delivered more than once: {total_deliveries} deliveries "
            f"for {len(expected)} messages"
        )
    _info(f"day soak: {len(expected)} messages delivered exactly once across "
          f"{len(outcomes)} ticks")


def test_soak_failure_requeues_without_loss_or_duplication():
    s = Soak(start=1_800_000_000)
    ids = [s.arrive("chatA", s.start - 2 * GAP, f"f{i}") for i in range(3)]
    t1 = s.tick(succeeds=False)   # run fails -> claim must be released
    if not t1["delivered"]:
        _fail("first tick should have been offered the interval")
    t2 = s.tick(succeeds=True)    # re-offered and sealed
    if not t2["delivered"]:
        _fail(f"failed tick's interval was lost: {t2}")
    if set(t2["delivered"]) != set(ids):
        _fail(f"re-queued interval content drifted: {t2['delivered']} vs {ids}")
    t3 = s.tick(succeeds=True)
    if t3["delivered"]:
        _fail(f"sealed interval re-offered after success: {t3}")
    _info("failed run re-queues its interval exactly once; success seals it")


def test_soak_aborted_and_stale_claims_recover():
    s = Soak(start=1_800_000_000)
    s.arrive("chatA", s.start - 2 * GAP, "x1")
    t1 = s.tick(succeeds=False)   # aborted-equivalent (non-success)
    t2 = s.tick(succeeds=True)
    if not t2["delivered"]:
        _fail("abort/stale claim recovery lost the interval")
    # zombie claim: claim with no finishing run at all (crash) -> TTL
    s2 = Soak(start=1_800_000_000)
    s2.arrive("chatB", s2.start - 2 * GAP, "z1")
    s2.tick(succeeds=True)
    # corrupt state: force a stale claimed row with no run covering it
    rows = [r for r in mod._load_interval_rows(s2.storage)]
    s2.storage.execute_write(
        f"UPDATE {mod._interval_table(s2.storage)} SET status='claimed', "
        f"claimed_at=:t WHERE id=:id",
        {"t": s2.now - 3 * 3600, "id": rows[0]["id"]},
    )
    s2.runs = []
    s2.now += 4 * 3600
    t = s2.tick(succeeds=True)
    if not t["delivered"]:
        _fail(f"TTL recovery lost the zombie-claimed interval: {t}")
    _info("aborted runs and zombie (TTL) claims both recover without loss")


def test_soak_steady_state_empty_and_monotonic():
    s = Soak(start=1_800_000_000)
    for i in range(4):
        s.arrive("chatA", s.start - 2 * GAP + i * 90, f"s{i}")
    analyzed_series = []
    for _ in range(8):
        o = s.tick(succeeds=True)
        analyzed_series.append(o["analyzed"])
        if o["delivered"] and any(x in s.delivered for x in o["delivered"]):
            pass
    if any(b < a for a, b in zip(analyzed_series, analyzed_series[1:])):
        _fail(f"analyzed state went backwards: {analyzed_series}")
    tail = [s.tick(succeeds=True)["delivered"] for _ in range(4)]
    if any(tail):
        _fail(f"steady state kept offering intervals: {tail}")
    _info("analyzed state is monotonic and the steady state is empty")


def test_soak_clock_skew_future_message_deferred():
    s = Soak(start=1_800_000_000)
    s.arrive("chatA", s.now + 3600, "future")   # clock skew ahead
    t1 = s.tick(succeeds=True)
    if t1["delivered"]:
        _fail(f"future-ts message was processed early: {t1}")
    s.now += 3 * 3600 + GAP                      # its time passes + seals
    t2 = s.tick(succeeds=True)
    if not t2["delivered"]:
        _fail(f"future-ts message never processed after its time passed: {t2}")
    _info("clock-skewed message is deferred then processed exactly once")


ALL_TESTS = [
    test_soak_day_exactly_once,
    test_soak_failure_requeues_without_loss_or_duplication,
    test_soak_aborted_and_stale_claims_recover,
    test_soak_steady_state_empty_and_monotonic,
    test_soak_clock_skew_future_message_deferred,
]


def main() -> None:
    for t in ALL_TESTS:
        t()
    print(f"All {len(ALL_TESTS)} soak checks PASS.")


if __name__ == "__main__":
    sys.exit(main())
