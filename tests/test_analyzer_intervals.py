"""Adversarial tests for the conversation-interval analysis queue.

The queue is the harness's answer to duplicate findings: intervals are
computed from messages, claimed when handed to a run and sealed only when
that run finishes with success. These tests try to break every boundary
of that state machine — gap/ seal edges, late-arriving messages, failed
and aborted runs, TTL crashes, clock skew, adoption — because any hole
here becomes a duplicate (or a lost finding) in production.

Run with::

    PYTHONPATH=/Users/maosone/Documents/odysseus python3 tests/test_analyzer_intervals.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time

sys.path.insert(0, "/Users/maosone/Documents/odysseus")

EXT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "extensions",
    "whats2api_bridge",
)

_spec = importlib.util.spec_from_file_location(
    "analyzer_plugin_under_test", os.path.join(EXT_DIR, "plugin.py")
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

GAP = 45 * 60  # default split/seal gap used throughout


def _info(msg: str) -> None:
    print(f"  [ok] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    raise SystemExit(1)


def _msg(chat, ts, text="t", mid=None):
    return {
        "id": mid or f"{chat}-{ts}",
        "chat_jid": chat,
        "sender_jid": chat,
        "push_name": "Tester",
        "text": text,
        "message_type": "text",
        "ts": ts,
    }


def _storage():
    from sqlalchemy import create_engine
    from core.extensions.storage import PluginStorage

    engine = create_engine(
        f"sqlite:///{tempfile.mkdtemp(prefix='w2a-intervals-')}/app.db"
    )
    storage = PluginStorage("whats2api_bridge", engine=engine)
    storage.ensure_table(
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
    storage.ensure_table(
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
    return storage


# ── compute_analysis_queue: gap / seal boundaries ─────────────────────────


def test_gap_splits_conversation():
    now = 10**10
    msgs = [_msg("c", now - 3 * GAP), _msg("c", now - 3 * GAP + 60),
            _msg("c", now - GAP - 60)]
    new, done = mod.compute_analysis_queue(msgs, [], now=now, gap_seconds=GAP)
    if len(new) != 2 or done:
        _fail(f"expected 2 intervals, got {len(new)} new / {len(done)} done")
    if new[0]["msg_count"] != 2 or new[1]["msg_count"] != 1:
        _fail(f"wrong grouping: {[i['msg_count'] for i in new]}")
    _info("silent gap splits a conversation into two intervals")


def test_gap_boundary_exact_threshold_splits():
    now = 10**10
    a, b = now - 3 * GAP, now - 3 * GAP + GAP  # gap exactly GAP
    new, _ = mod.compute_analysis_queue(
        [_msg("c", a), _msg("c", b)], [], now=now, gap_seconds=GAP
    )
    if len(new) != 2:
        _fail(f"exact-threshold gap must split, got {len(new)}")
    _info("gap exactly at threshold splits (>= is a new interval)")


def test_gap_just_under_threshold_merges():
    now = 10**10
    a, b = now - 3 * GAP, now - 3 * GAP + GAP - 1
    new, _ = mod.compute_analysis_queue(
        [_msg("c", a), _msg("c", b)], [], now=now, gap_seconds=GAP
    )
    if len(new) != 1 or new[0]["msg_count"] != 2:
        _fail(f"sub-threshold gap must merge, got {new}")
    _info("gap just under threshold merges")


def test_recent_group_deferred_unsealed():
    now = 10**10
    msgs = [_msg("c", now - 60), _msg("c", now - 30)]  # too recent
    new, _ = mod.compute_analysis_queue(msgs, [], now=now, gap_seconds=GAP)
    if new:
        _fail(f"unsealed (still growing) group must be deferred: {new}")
    _info("group quieter than the gap is deferred (cannot grow mid-analysis)")


def test_seal_boundary_exactly_gap_old_included():
    now = 10**10
    msgs = [_msg("c", now - GAP)]  # quiet exactly one gap
    new, _ = mod.compute_analysis_queue(msgs, [], now=now, gap_seconds=GAP)
    if len(new) != 1:
        _fail(f"exactly-gap-old group must be sealed: {new}")
    _info("group quiet for exactly one gap is sealed")


def test_future_message_deferred():
    now = 10**10
    msgs = [_msg("c", now + 300)]  # clock skew
    new, _ = mod.compute_analysis_queue(msgs, [], now=now, gap_seconds=GAP)
    if new:
        _fail(f"future-ts message must not be sealed: {new}")
    _info("future-ts message (clock skew) is deferred, never sealed")


def test_empty_messages():
    new, done = mod.compute_analysis_queue([], [], now=10**10, gap_seconds=GAP)
    if new or done:
        _fail("empty input must yield an empty queue")
    _info("no messages -> empty queue")


def test_single_old_message_yields_interval():
    now = 10**10
    new, _ = mod.compute_analysis_queue(
        [_msg("c", now - 2 * GAP)], [], now=now, gap_seconds=GAP
    )
    if len(new) != 1 or new[0]["msg_count"] != 1:
        _fail(f"single old message must yield one interval: {new}")
    _info("single old message yields one interval")


# ── compute_analysis_queue: coverage / late messages ──────────────────────


def test_analyzed_range_covers_messages():
    now = 10**10
    t0 = now - 4 * GAP
    analyzed = [{"chat_jid": "c", "start_ts": t0, "end_ts": t0 + 600,
                 "findings_summary": "done", "analyzed_at": 1}]
    msgs = [_msg("c", t0 + 30), _msg("c", t0 + 300)]  # both covered
    new, done = mod.compute_analysis_queue(msgs, analyzed, now=now, gap_seconds=GAP)
    if new:
        _fail(f"covered messages must not be re-offered: {new}")
    if len(done) != 1:
        _fail("done report must keep the analyzed interval")
    _info("analyzed range suppresses its messages (no reprocessing)")


def test_late_message_inside_analyzed_range_ignored():
    now = 10**10
    t0 = now - 4 * GAP
    analyzed = [{"chat_jid": "c", "start_ts": t0, "end_ts": t0 + 600,
                 "findings_summary": "done", "analyzed_at": 1}]
    late = _msg("c", t0 + 300)  # ts inside the analyzed range, delivered later
    new, _ = mod.compute_analysis_queue([late], analyzed, now=now, gap_seconds=GAP)
    if new:
        _fail(f"late message inside an analyzed range must be ignored: {new}")
    _info("late arrival inside an analyzed interval is ignored")


def test_late_message_after_analyzed_range_new_mini_interval():
    now = 10**10
    t0 = now - 4 * GAP
    analyzed = [{"chat_jid": "c", "start_ts": t0, "end_ts": t0 + 600,
                 "findings_summary": "done", "analyzed_at": 1}]
    late = _msg("c", t0 + 600 + GAP + 1)  # beyond the range and the gap
    new, _ = mod.compute_analysis_queue([late], analyzed, now=now, gap_seconds=GAP)
    if len(new) != 1 or new[0]["msg_count"] != 1:
        _fail(f"late message past the range must form a mini-interval: {new}")
    _info("late arrival past an analyzed interval forms a new mini-interval")


def test_unseen_messages_around_covered_range_group_without_covered():
    now = 10**10
    t0 = now - 5 * GAP
    analyzed = [{"chat_jid": "c", "start_ts": t0 + 100, "end_ts": t0 + 120,
                 "findings_summary": "done", "analyzed_at": 1}]
    msgs = [_msg("c", t0 + 50, mid="a"), _msg("c", t0 + 110, mid="covered"),
            _msg("c", t0 + 200, mid="b")]
    new, _ = mod.compute_analysis_queue(msgs, analyzed, now=now, gap_seconds=GAP)
    if len(new) != 1:
        _fail(f"unseen messages must merge around the covered one: {new}")
    ids = [m["id"] for m in new[0]["messages"]]
    if ids != ["a", "b"] or "covered" in ids:
        _fail(f"covered message leaked into the queue: {ids}")
    _info("covered message inside a cluster is excluded, its neighbours merge")


def test_interval_id_stable():
    now = 10**10
    msgs = [_msg("c", now - 3 * GAP), _msg("c", now - 3 * GAP + 60)]
    a, _ = mod.compute_analysis_queue(msgs, [], now=now, gap_seconds=GAP)
    b, _ = mod.compute_analysis_queue(list(msgs), [], now=now, gap_seconds=GAP)
    if a[0]["id"] != b[0]["id"]:
        _fail("interval id must be deterministic for the same content")
    _info("interval ids are deterministic")


# ── seal_claims: run lifecycle ────────────────────────────────────────────


NOW = 1_800_000_000  # realistic epoch scale for all run/claim fixtures


def _claim(row_id="iv1", claimed_at=None):
    return {
        "id": row_id, "status": "claimed",
        "claimed_at": NOW - 600 if claimed_at is None else claimed_at,
        "chat_jid": "c", "start_ts": 1, "end_ts": 2, "msg_count": 1,
    }


def _run(**kw):
    base = {"id": "r1", "started_at": NOW - 700, "finished_at": NOW - 100,
            "status": "success", "result": "Findings (chat: c): task X"}
    base.update(kw)
    return base


def test_success_run_seals_claim_with_summary():
    d = mod.seal_claims([_claim()], [_run()], now=NOW)
    if len(d["seal"]) != 1 or d["release"] or d["keep"]:
        _fail(f"success must seal: {d}")
    row, run = d["seal"][0]
    if "Findings" not in mod._run_result_excerpt(run["result"]):
        _fail("seal must carry the run result excerpt as summary")
    _info("claim during a successful run is sealed with the result excerpt")


def test_failed_run_releases_claim():
    d = mod.seal_claims([_claim()], [_run(status="error", result="")], now=NOW)
    if len(d["release"]) != 1 or d["seal"] or d["keep"]:
        _fail(f"failed run must release its claim: {d}")
    _info("claim of a failed run is released (no silent loss)")


DISCLOSURE_ONLY = (
    "External operation mcp__oxs_whats2api_bridge__get_analysis_queue reported "
    "success; any external change it made was not independently verified."
)


def test_disclosure_only_success_releases_claim_instead_of_sealing_it():
    """A run that only emitted the effect disclosure never read the interval.

    ``status == success`` used to be the whole seal condition, so a model that
    answered from the prompt without calling the queue — or whose answer was
    replaced by the disclosure — sealed the interval for good. Its promises
    never became findings and no later run saw the content again.
    """
    d = mod.seal_claims([_claim()], [_run(result=DISCLOSURE_ONLY)], now=NOW)
    if len(d["release"]) != 1 or d["seal"] or d["keep"]:
        _fail(f"disclosure-only run must release, not seal: {d}")
    _info("success that only emitted the effect disclosure releases its claim")


def test_empty_result_releases_claim():
    d = mod.seal_claims([_claim()], [_run(result="")], now=NOW)
    if len(d["release"]) != 1 or d["seal"] or d["keep"]:
        _fail(f"empty-result run must release, not seal: {d}")
    _info("success with an empty result releases its claim")


def test_real_answer_next_to_the_disclosure_still_seals():
    d = mod.seal_claims(
        [_claim()], [_run(result="No new conversation intervals.\n\n" + DISCLOSURE_ONLY)],
        now=NOW,
    )
    if len(d["seal"]) != 1:
        _fail(f"a real answer beside the disclosure must still seal: {d}")
    _info("real answer + disclosure still seals (disclosure is not the whole result)")


def test_aborted_run_releases_claim():
    d = mod.seal_claims(
        [_claim()], [_run(status="aborted", result="Starting…")], now=NOW
    )
    if len(d["release"]) != 1:
        _fail(f"aborted run must release its claim: {d}")
    _info("claim of an aborted (concurrent) run is released")


def test_inflight_run_keeps_claim():
    d = mod.seal_claims(
        [_claim()], [_run(finished_at=None, status="running", result="")],
        now=NOW,
    )
    if len(d["keep"]) != 1 or d["seal"] or d["release"]:
        _fail(f"in-flight run must keep its claim: {d}")
    _info("claim of an in-flight run is kept")


def test_stale_claim_released_without_runs():
    d = mod.seal_claims(
        [_claim(claimed_at=NOW - 3 * 3600)], [], now=NOW
    )
    if len(d["release"]) != 1:
        _fail(f"stale claim must be released by TTL: {d}")
    _info("stale claim (crashed run) is released by TTL")


def test_stale_claim_released_even_if_run_still_marked_running():
    # A crashed run leaves status='running' forever; TTL must not let it
    # black-hole the interval.
    d = mod.seal_claims(
        [_claim(claimed_at=NOW - 3 * 3600)],
        [_run(started_at=NOW - 3 * 3600 - 60, finished_at=None,
              status="running", result="")],
        now=NOW,
    )
    if len(d["release"]) != 1:
        _fail(f"stale claim of a zombie run must be released: {d}")
    _info("zombie 'running' run cannot black-hole an interval past TTL")


def test_claim_after_run_window_not_sealed():
    # The claim belongs to the CURRENT (in-flight) run; an earlier success
    # must not steal it.
    d = mod.seal_claims([_claim(claimed_at=NOW - 30)], [_run()], now=NOW)
    if d["seal"]:
        _fail(f"claim outside the run window must not be sealed: {d}")
    if len(d["keep"]) != 1:
        _fail(f"claim of the current run must be kept: {d}")
    _info("a success cannot seal claims made after it finished")


def test_nonclaimed_rows_ignored():
    row = {"id": "iv1", "status": "analyzed", "claimed_at": 1}
    d = mod.seal_claims([row], [], now=10**10)
    if any(d.values()):
        _fail(f"analyzed rows must not be re-decided: {d}")
    _info("analyzed rows are left alone")


# ── full state cycle on storage (the anti-duplicate property) ─────────────


def test_claim_seal_recompute_no_duplicates():
    storage = _storage()
    now = 10**10
    t0 = now - 3 * GAP
    for m in [_msg("c", t0), _msg("c", t0 + 60)]:
        storage.insert("messages", connection_id="x", direction="in", **m)

    msgs = mod._fetch_window_messages(storage, 0)
    new, _ = mod.compute_analysis_queue(msgs, [], now=now, gap_seconds=GAP)
    if len(new) != 1:
        _fail(f"first pass must offer the interval once: {new}")
    mod._claim_intervals(storage, new, now=now)

    runs = [{"id": "r1", "started_at": now - 10, "finished_at": now,
             "status": "success", "result": "task X — created"}]
    claimed = [r for r in mod._load_interval_rows(storage)
               if r["status"] == "claimed"]
    mod._apply_seal_decisions(storage, mod.seal_claims(claimed, runs, now=now))

    covered = [r for r in mod._load_interval_rows(storage)
               if r["status"] in ("analyzed", "claimed")]
    msgs2 = mod._fetch_window_messages(storage, 0)
    new2, done2 = mod.compute_analysis_queue(msgs2, covered, now=now,
                                             gap_seconds=GAP)
    if new2:
        _fail(f"second pass must be empty (duplicates would appear): {new2}")
    if len(done2) != 1 or not (done2[0].get("findings_summary") or ""):
        _fail(f"done report must carry the sealed summary: {done2}")
    _info("claim -> success seal -> recompute offers nothing (no duplicates)")


def test_failed_run_cycle_requeues():
    storage = _storage()
    now = 10**10
    t0 = now - 3 * GAP
    storage.insert("messages", connection_id="x", direction="in",
                   **_msg("c", t0))
    msgs = mod._fetch_window_messages(storage, 0)
    new, _ = mod.compute_analysis_queue(msgs, [], now=now, gap_seconds=GAP)
    mod._claim_intervals(storage, new, now=now)

    runs = [{"id": "r1", "started_at": now - 10, "finished_at": now,
             "status": "error", "result": ""}]
    claimed = [r for r in mod._load_interval_rows(storage)
               if r["status"] == "claimed"]
    mod._apply_seal_decisions(storage, mod.seal_claims(claimed, runs, now=now))

    covered = [r for r in mod._load_interval_rows(storage)
               if r["status"] in ("analyzed", "claimed")]
    msgs2 = mod._fetch_window_messages(storage, 0)
    new2, _ = mod.compute_analysis_queue(msgs2, covered, now=now,
                                         gap_seconds=GAP)
    if len(new2) != 1:
        _fail(f"failed run must re-queue its interval: {new2}")
    _info("failed run re-queues its interval on the next pass")


# ── adoption (pre-intervalization migration) ──────────────────────────────


def test_adoption_marks_sealed_analyzed_and_runs_once():
    storage = _storage()
    now = 10**10
    t0 = now - 3 * GAP
    storage.insert("messages", connection_id="x", direction="in",
                   **_msg("old", t0))
    n = mod._adopt_existing_intervals(
        storage, now=now, gap_seconds=GAP, tracking_since=now,
    )
    if n != 1:
        _fail(f"adoption must mark the sealed interval analyzed: {n}")
    rows = mod._load_interval_rows(storage)
    if rows[0]["status"] != "analyzed":
        _fail(f"adopted row must be analyzed: {rows}")
    if mod._adopt_existing_intervals(
            storage, now=now, gap_seconds=GAP, tracking_since=now) != 0:
        _fail("adoption must be a one-shot (state table no longer empty)")
    _info("adoption marks pre-existing sealed content analyzed (once)")


def test_adoption_keeps_unsealed_groups():
    storage = _storage()
    now = 10**10
    storage.insert("messages", connection_id="x", direction="in",
                   **_msg("fresh", now - 60))
    n = mod._adopt_existing_intervals(
        storage, now=now, gap_seconds=GAP, tracking_since=now,
    )
    if n != 0:
        _fail(f"unsealed group must not be adopted: {n}")
    msgs = mod._fetch_window_messages(storage, 0)
    new, _ = mod.compute_analysis_queue(msgs, [], now=now, gap_seconds=GAP)
    if new:
        _fail(f"unsealed group must stay out of the queue until sealed: {new}")
    _info("adoption leaves recent (unsealed) groups for the first real run")


def test_adoption_never_seals_content_that_arrived_after_tracking_started():
    """The silent-loss case the E2E hit on a live container.

    Adoption used to key only on "the state table is empty", which it read
    as "nothing has ever been tracked". A fresh container therefore sealed
    every message already in the window as
    ``adopted: pre-intervalization content`` — including one that had just
    arrived — and the analyzer saw an empty queue with no error anywhere.
    """
    storage = _storage()
    now = 10**10
    tracked_since = now - 4 * GAP
    # One settled message on each side of the boundary, on distinct chats.
    storage.insert("messages", connection_id="x", direction="in",
                   **_msg("pre@s.whatsapp.net", now - 5 * GAP))
    storage.insert("messages", connection_id="x", direction="in",
                   **_msg("post@s.whatsapp.net", now - 3 * GAP))

    n = mod._adopt_existing_intervals(
        storage, now=now, gap_seconds=GAP, tracking_since=tracked_since,
    )
    if n != 1:
        _fail(f"only pre-tracking content may be adopted: {n}")

    adopted = mod._load_interval_rows(storage)
    chats = sorted(r.get("chat_jid") for r in adopted)
    if chats != ["pre@s.whatsapp.net"]:
        _fail(f"adoption must touch only the pre-tracking chat: {chats}")

    msgs = mod._fetch_window_messages(storage, 0)
    new, _ = mod.compute_analysis_queue(msgs, adopted, now=now, gap_seconds=GAP)
    chats = sorted(iv["chat_jid"] for iv in new)
    if chats != ["post@s.whatsapp.net"]:
        _fail(
            "post-tracking content must still be offered to the analyzer; "
            f"got {chats}"
        )
    _info("adoption seals only what predates interval tracking")


def test_adoption_refuses_to_run_without_a_boundary():
    """No boundary recorded → skip adoption rather than guess.

    An unbounded adoption can only lose data; a skipped one is re-offered to
    the model, which is recoverable. Failing open here is the bug.
    """
    storage = _storage()
    now = 10**10
    storage.insert("messages", connection_id="x", direction="in",
                   **_msg("anything", now - 5 * GAP))
    n = mod._adopt_existing_intervals(storage, now=now, gap_seconds=GAP)
    if n != 0:
        _fail(f"adoption without a boundary must not seal anything: {n}")
    if mod._load_interval_rows(storage):
        _fail("adoption without a boundary must leave the table empty")
    msgs = mod._fetch_window_messages(storage, 0)
    new, _ = mod.compute_analysis_queue(msgs, [], now=now, gap_seconds=GAP)
    if len(new) != 1:
        _fail(f"content must stay available instead of being sealed: {new}")
    _info("adoption without a tracking boundary seals nothing")


ALL_TESTS = [
    test_gap_splits_conversation,
    test_gap_boundary_exact_threshold_splits,
    test_gap_just_under_threshold_merges,
    test_recent_group_deferred_unsealed,
    test_seal_boundary_exactly_gap_old_included,
    test_future_message_deferred,
    test_empty_messages,
    test_single_old_message_yields_interval,
    test_analyzed_range_covers_messages,
    test_late_message_inside_analyzed_range_ignored,
    test_late_message_after_analyzed_range_new_mini_interval,
    test_unseen_messages_around_covered_range_group_without_covered,
    test_interval_id_stable,
    test_success_run_seals_claim_with_summary,
    test_failed_run_releases_claim,
    test_disclosure_only_success_releases_claim_instead_of_sealing_it,
    test_empty_result_releases_claim,
    test_real_answer_next_to_the_disclosure_still_seals,
    test_aborted_run_releases_claim,
    test_inflight_run_keeps_claim,
    test_stale_claim_released_without_runs,
    test_stale_claim_released_even_if_run_still_marked_running,
    test_claim_after_run_window_not_sealed,
    test_nonclaimed_rows_ignored,
    test_claim_seal_recompute_no_duplicates,
    test_failed_run_cycle_requeues,
    test_adoption_marks_sealed_analyzed_and_runs_once,
    test_adoption_keeps_unsealed_groups,
    test_adoption_never_seals_content_that_arrived_after_tracking_started,
    test_adoption_refuses_to_run_without_a_boundary,
]


def main() -> None:
    for t in ALL_TESTS:
        t()
    print(f"All {len(ALL_TESTS)} interval-queue checks PASS.")


if __name__ == "__main__":
    sys.exit(main())
