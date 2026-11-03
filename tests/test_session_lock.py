#!/usr/bin/env python3
"""Tests for session_lock.py -- tri-state session-worktree liveness (#1588).

Run: python -m pytest tests/test_session_lock.py -q
(NEVER `python test_session_lock.py` -- a pytest file run directly exits 0 doing
nothing; feedback-pytest-file-false-pass-direct-run.)

The regression under test: CLAUDE.md's lock recipe recorded `os.getppid()` from
a Bash-tool call on the false premise that the parent shell is long-lived. It is
not -- Claude Code spawns a fresh shell per tool call -- so EVERY lock's PID was
dead within seconds. That made every worktree read *unoccupied* (defeating the
occupation guardrail) and made the post-compaction re-anchor unreachable.

The ancestor walk is exercised against SYNTHETIC process tables so the assertions
are deterministic on any host (feedback-ci-checks-cross-platform); the real
`snapshot_processes()` is separately smoke-tested against the live process table.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SCRIPTS = _HERE.parents[1] / "scripts"
_SCRIPT = _SCRIPTS / "session_lock.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sl = _load("session_lock", _SCRIPT)

# Far above any OS pid_max (Linux default ~4M) -- guaranteed dead on both
# platforms, so the dead-path assertions are non-flaky.
DEAD_PID = 2147483647

# A synthetic chain: python (leaf) -> bash -> node (the Claude Code session).
CHAIN = [
    {"pid": 100, "ppid": 200, "image": "python.exe"},
    {"pid": 200, "ppid": 300, "image": "bash.exe"},
    {"pid": 300, "ppid": 400, "image": "node.exe"},      # <- the session process
    {"pid": 400, "ppid": 0, "image": "explorer.exe"},
]


def _run(*args):
    return subprocess.run([sys.executable, str(_SCRIPT), *args],
                          capture_output=True, text=True)


def _own_image():
    """This process's image name AS THE SNAPSHOT REPORTS IT.

    Not `basename(sys.executable)`: Linux `/proc/<pid>/stat` truncates `comm` to
    15 chars ("python3.12" vs "python3"), so deriving the name from the snapshot
    is the only way these tests bind to a genuinely live process on every host.
    """
    me = os.getpid()
    for p in sl.snapshot_processes():
        if p["pid"] == me:
            return sl._image_key(p["image"])
    raise AssertionError("this process is not in its own process table")


def _write_legacy(path, pid, session_id="sess-test"):
    """The pre-#1588 lock shape: {pid, session_id}, no pid_kind marker."""
    path.write_text(json.dumps({"pid": pid, "session_id": session_id}),
                    encoding="utf-8")


def _write_modern(path, pid, session_id="sess-test", pid_kind=sl.KIND_SESSION):
    path.write_text(json.dumps({
        "schema": sl.SCHEMA, "pid": pid, "pid_kind": pid_kind,
        "session_id": session_id, "created": "2026-07-13T10:00:00+00:00",
    }), encoding="utf-8")


def _write_modern_hb(path, pid, session_id="sess-test", pid_kind=sl.KIND_SESSION,
                      heartbeat=None, created="2026-07-13T10:00:00+00:00"):
    """Like `_write_modern`, but with an explicit (or omitted) `heartbeat` field
    -- for the #1435 freshness-veto tests, which need to control staleness
    independently of `created`."""
    payload = {
        "schema": sl.SCHEMA, "pid": pid, "pid_kind": pid_kind,
        "session_id": session_id, "created": created,
    }
    if heartbeat is not None:
        payload["heartbeat"] = heartbeat
    path.write_text(json.dumps(payload), encoding="utf-8")


# A heartbeat this old is stale under any sane threshold; used to force the
# "stale" branch of the freshness veto deterministically.
STALE_HEARTBEAT = "2020-01-01T00:00:00+00:00"


# ─── ancestor walk ───────────────────────────────────────────────────────────


def test_resolve_session_pid_walks_up_to_the_session_process():
    pid, kind = sl.resolve_session_pid(start_pid=100, snapshot=CHAIN)
    assert (pid, kind) == (300, sl.KIND_SESSION)


def test_resolve_session_pid_never_returns_the_transient_shell():
    # The whole bug: the Bash-tool shell (200) must never be what gets recorded.
    pid, _ = sl.resolve_session_pid(start_pid=100, snapshot=CHAIN)
    assert pid != 200


def test_resolve_session_pid_unresolved_when_no_session_ancestor():
    chain = [
        {"pid": 100, "ppid": 200, "image": "python.exe"},
        {"pid": 200, "ppid": 0, "image": "bash.exe"},
    ]
    assert sl.resolve_session_pid(start_pid=100, snapshot=chain) == \
        (None, sl.KIND_UNRESOLVED)


def test_resolve_session_pid_unresolved_on_empty_process_table():
    assert sl.resolve_session_pid(start_pid=100, snapshot=[]) == \
        (None, sl.KIND_UNRESOLVED)


def test_resolve_session_pid_survives_a_cycle():
    # A malformed table must terminate the walk, not hang it.
    cyc = [{"pid": 1, "ppid": 2, "image": "a"}, {"pid": 2, "ppid": 1, "image": "b"}]
    assert sl.resolve_session_pid(start_pid=1, snapshot=cyc) == \
        (None, sl.KIND_UNRESOLVED)


def test_resolve_session_pid_matches_posix_image_names():
    chain = [
        {"pid": 10, "ppid": 20, "image": "python3"},
        {"pid": 20, "ppid": 30, "image": "bash"},
        {"pid": 30, "ppid": 1, "image": "node"},
    ]
    assert sl.resolve_session_pid(start_pid=10, snapshot=chain) == (30, sl.KIND_SESSION)


def test_snapshot_processes_sees_this_process():
    # Smoke test of the real platform enumeration (toolhelp32 / /proc / ps).
    snap = sl.snapshot_processes()
    assert snap, "process table came back empty"
    assert os.getpid() in {p["pid"] for p in snap}


# ─── criterion 1: a lock written by the new writer for the RUNNING session ───


def test_written_lock_for_running_session_reads_alive(tmp_path):
    """AC1. The ancestor walk is pointed at the running interpreter's own image,
    so it resolves to a genuinely LIVE process on any host -- the same mechanism
    the real writer uses, minus the dependency on a `node` ancestor that a CI
    runner does not have."""
    lock = tmp_path / ".session-lock"
    written = sl.write_lock(lock, "sess-mine", session_images={_own_image()})

    assert written["pid_kind"] == sl.KIND_SESSION
    assert written["pid"] > 0
    assert written["schema"] == sl.SCHEMA
    assert written["created"]
    assert sl.lock_liveness(lock) == sl.ALIVE
    assert sl.lock_liveness(lock, "sess-mine") == sl.ALIVE


def test_writer_records_null_pid_when_session_unresolvable(tmp_path):
    """Fail-safe, not a lie: with no session ancestor the writer records
    pid=null / "unresolved" rather than the transient shell's pid."""
    lock = tmp_path / ".session-lock"
    written = sl.write_lock(lock, "sess-mine", session_images={"no-such-image"})

    assert written["pid"] is None
    assert written["pid_kind"] == sl.KIND_UNRESOLVED
    assert sl.lock_liveness(lock) == sl.UNKNOWN


# ─── criteria 2 + 3: DEAD only with proof; LEGACY dead pid is UNKNOWN ────────


def test_dead_session_lifetime_pid_reads_dead(tmp_path):
    """AC2. pid_kind proves the pid was session-lifetime -> a dead pid is proof
    the session is gone."""
    lock = tmp_path / ".session-lock"
    _write_modern(lock, DEAD_PID)
    assert sl.lock_liveness(lock) == sl.DEAD


def test_legacy_lock_with_dead_pid_reads_unknown_not_dead(tmp_path):
    """AC3 -- THE regression that defeated the occupation guardrail.

    A legacy `{pid, session_id}` lock with a dead pid is ambiguous: it is either
    an exited session, or a LIVE session whose lock was written by the broken
    pre-#1588 recipe (transient Bash-shell pid, dead on arrival). Reading it as
    a confident DEAD is what cleared a concurrent session to move into a worktree
    that was in active use."""
    lock = tmp_path / ".session-lock"
    _write_legacy(lock, DEAD_PID)
    assert sl.lock_liveness(lock) == sl.UNKNOWN


def test_legacy_lock_with_live_pid_reads_alive(tmp_path):
    # A live pid is unambiguous whatever the format wrote it.
    lock = tmp_path / ".session-lock"
    _write_legacy(lock, os.getpid())
    assert sl.lock_liveness(lock) == sl.ALIVE


def test_unresolved_lock_reads_unknown(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, None, pid_kind=sl.KIND_UNRESOLVED)
    assert sl.lock_liveness(lock) == sl.UNKNOWN


def test_missing_lock_reads_dead(tmp_path):
    # No lock == no claim. (Keeps GC functional; a claim must be made to be kept.)
    assert sl.lock_liveness(tmp_path / "nope.json") == sl.DEAD


def test_malformed_lock_reads_dead(tmp_path):
    lock = tmp_path / ".session-lock"
    lock.write_text("not-json{", encoding="utf-8")
    assert sl.lock_liveness(lock) == sl.DEAD


def test_lock_without_pid_field_reads_dead(tmp_path):
    lock = tmp_path / ".session-lock"
    lock.write_text(json.dumps({"session_id": "x"}), encoding="utf-8")
    assert sl.lock_liveness(lock) == sl.DEAD


def test_session_id_mismatch_reads_dead(tmp_path):
    # "Is this MY live lock?" -- someone else's lock is DEAD *to me*. This is the
    # composite-key guard against a recycled PID (ADR Decision 2).
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id="other-session")
    assert sl.lock_liveness(lock, "sess-mine") == sl.DEAD


# ─── criterion 4: the two consumer queries disagree in opposite directions ───


def test_unknown_is_occupied(tmp_path):
    """AC4a. Never hand out a worktree you cannot PROVE is free."""
    lock = tmp_path / ".session-lock"
    _write_legacy(lock, DEAD_PID, session_id="someone-else")
    assert sl.lock_liveness(lock) == sl.UNKNOWN
    assert sl.occupancy(lock) == "OCCUPIED"
    assert sl.occupancy(lock, self_session_id="sess-mine") == "OCCUPIED"


def test_unknown_is_not_reapable(tmp_path):
    """AC4b. Never reap a worktree you cannot PROVE is dead."""
    lock = tmp_path / ".session-lock"
    _write_legacy(lock, DEAD_PID)
    assert sl.reapable(lock) is False


def test_alive_is_occupied_and_not_reapable(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id="someone-else")
    assert sl.occupancy(lock) == "OCCUPIED"
    assert sl.reapable(lock) is False


def test_proven_dead_is_free_and_reapable(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, DEAD_PID)
    assert sl.occupancy(lock) == "FREE"
    assert sl.reapable(lock) is True


def test_my_own_lock_is_not_occupied_by_someone_else(tmp_path):
    # Re-entering my own worktree is always allowed -- the guard is about OTHERS.
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id="sess-mine")
    assert sl.occupancy(lock, self_session_id="sess-mine") == "FREE"
    assert sl.occupancy(lock, self_session_id="sess-other") == "OCCUPIED"


def test_no_lock_is_free_and_reapable(tmp_path):
    assert sl.occupancy(tmp_path / "nope.json") == "FREE"
    assert sl.reapable(tmp_path / "nope.json") is True


# ─── #2843: a same-process prior-session lock (left behind by /clear) ────────
#
# `/clear` keeps the Claude Code process (same session-lifetime pid) and hands
# out a new CLAUDE_CODE_SESSION_ID. The worktree's lock still names the
# pre-clear id on that LIVE pid, so the successor read its own worktree as
# OCCUPIED and was refused. The caller's own resolved session pid is injected
# (`self_pid`) so every case is deterministic on any host.

OLD_ID = "sess-before-clear"
NEW_ID = "sess-after-clear"


def test_same_process_prior_session_lock_is_free_to_its_successor(tmp_path):
    """The fix: the lock's session-lifetime pid IS the caller's own session
    process, under a different id -> a session this process hosted earlier."""
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id=OLD_ID)    # live pid -> ALIVE
    assert sl.liveness(sl.read_lock(lock)) == sl.ALIVE
    assert sl.occupancy(lock, self_session_id=NEW_ID,
                        self_pid=(os.getpid(), sl.KIND_SESSION)) == "FREE"


def test_different_live_process_lock_stays_occupied(tmp_path):
    """Clean path: another live session process keeps its worktree."""
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id="someone-else")
    assert sl.occupancy(lock, self_session_id=NEW_ID,
                        self_pid=(os.getpid() + 1, sl.KIND_SESSION)) == "OCCUPIED"


def test_same_process_exemption_needs_a_declared_self(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id=OLD_ID)
    assert sl.occupancy(lock, self_pid=(os.getpid(), sl.KIND_SESSION)) == "OCCUPIED"


def test_same_process_exemption_needs_a_session_kind_lock(tmp_path):
    """Only `pid_kind: "session"` proves the pid meant the session process; an
    unresolved or legacy lock keeps its pre-#2843 verdict."""
    me = (os.getpid(), sl.KIND_SESSION)
    unresolved = tmp_path / "unresolved.json"
    _write_modern(unresolved, os.getpid(), session_id=OLD_ID,
                  pid_kind=sl.KIND_UNRESOLVED)
    legacy = tmp_path / "legacy.json"
    _write_legacy(legacy, os.getpid(), session_id=OLD_ID)
    assert sl.occupancy(unresolved, self_session_id=NEW_ID, self_pid=me) == "OCCUPIED"
    assert sl.occupancy(legacy, self_session_id=NEW_ID, self_pid=me) == "OCCUPIED"


def test_same_process_exemption_needs_a_resolved_caller(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id=OLD_ID)
    assert sl.occupancy(lock, self_session_id=NEW_ID,
                        self_pid=(None, sl.KIND_UNRESOLVED)) == "OCCUPIED"
    assert sl.occupancy(lock, self_session_id=NEW_ID,
                        self_pid=(os.getpid(), sl.KIND_UNRESOLVED)) == "OCCUPIED"


def test_write_rekeys_a_same_process_prior_session_lock_without_force(tmp_path):
    """The hook's adopt path: `write` (no --force) over the pre-clear lock
    succeeds and records the successor's id on the same pid."""
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id=OLD_ID)
    written = sl.write_lock(lock, NEW_ID, session_images={_own_image()})
    assert written["pid"] == os.getpid()
    on_disk = json.loads(lock.read_text())
    assert (on_disk["session_id"], on_disk["pid"]) == (NEW_ID, os.getpid())


def test_write_still_refuses_a_different_live_process(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id="someone-else")
    try:
        sl.write_lock(lock, NEW_ID, pid=os.getpid() + 1, pid_kind=sl.KIND_SESSION)
        raise AssertionError("expected FileExistsError")
    except FileExistsError:
        pass
    assert json.loads(lock.read_text())["session_id"] == "someone-else"


# ─── heartbeat-freshness veto (#1435, spec-reaper-heartbeat-freshness-veto) ──


def test_make_lock_seeds_heartbeat_from_created():
    lock = sl.make_lock("sess-mine", pid=os.getpid(), pid_kind=sl.KIND_SESSION)
    assert lock["heartbeat"] == lock["created"]


def test_heartbeat_age_seconds_reads_the_heartbeat_field():
    age = sl.heartbeat_age_seconds({"created": STALE_HEARTBEAT,
                                    "heartbeat": STALE_HEARTBEAT})
    assert age is not None and age > 0


def test_heartbeat_age_seconds_falls_back_to_created_for_a_legacy_lock():
    # A lock predating this field has no `heartbeat` key at all.
    age = sl.heartbeat_age_seconds({"created": STALE_HEARTBEAT})
    assert age is not None and age > 0


def test_heartbeat_age_seconds_none_when_both_fields_absent():
    """AC5. Neither field present -- can't be proven stale."""
    assert sl.heartbeat_age_seconds({}) is None


def test_heartbeat_age_seconds_none_on_unparseable_values():
    assert sl.heartbeat_age_seconds(
        {"heartbeat": "not-a-date", "created": "also-not-a-date"}) is None


def test_touch_heartbeat_refreshes_own_lock(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id="sess-mine")
    before = json.loads(lock.read_text())

    updated = sl.touch_heartbeat(lock, "sess-mine")

    assert updated is not None
    assert updated["heartbeat"] != before["created"]   # actually refreshed
    assert updated["pid"] == before["pid"]
    assert updated["session_id"] == "sess-mine"
    assert updated["created"] == before["created"]     # untouched
    on_disk = json.loads(lock.read_text())
    assert on_disk["heartbeat"] == updated["heartbeat"]


def test_touch_heartbeat_refuses_a_foreign_session(tmp_path):
    """A session may only bump its OWN heartbeat."""
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id="other-session")
    before = lock.read_text()

    assert sl.touch_heartbeat(lock, "sess-mine") is None
    assert lock.read_text() == before   # file left untouched


def test_touch_heartbeat_missing_lock_returns_none(tmp_path):
    assert sl.touch_heartbeat(tmp_path / "nope.json", "sess-mine") is None


def test_reapable_fresh_dead_lock_is_kept(tmp_path):
    """AC1. DEAD liveness + a heartbeat refreshed a moment ago -> NOT reapable.
    The freshness veto sits ON TOP of liveness, not instead of it."""
    lock = tmp_path / ".session-lock"
    _write_modern(lock, DEAD_PID, session_id="sess-test")   # liveness: DEAD
    sl.touch_heartbeat(lock, "sess-test")                   # heartbeat: now
    assert sl.reapable(lock, stale_after_seconds=1800) is False


def test_reapable_stale_dead_lock_is_reapable(tmp_path):
    """AC2. Same DEAD lock, heartbeat old enough to cross the threshold."""
    lock = tmp_path / ".session-lock"
    _write_modern_hb(lock, DEAD_PID, heartbeat=STALE_HEARTBEAT)
    assert sl.reapable(lock, stale_after_seconds=1800) is True


def test_reapable_alive_lock_kept_regardless_of_stale_heartbeat(tmp_path):
    """AC3 -- liveness ALIVE wins outright; the freshness veto never OVER-reaps."""
    lock = tmp_path / ".session-lock"
    _write_modern_hb(lock, os.getpid(), heartbeat=STALE_HEARTBEAT)
    assert sl.reapable(lock, stale_after_seconds=1800) is False


def test_reapable_missing_heartbeat_and_created_is_kept_failsafe(tmp_path):
    """AC5. A DEAD lock with neither `heartbeat` nor `created` can't be proven
    stale -- fail-safe KEEP, mirroring UNKNOWN-is-alive for liveness."""
    lock = tmp_path / ".session-lock"
    lock.write_text(json.dumps({
        "schema": sl.SCHEMA, "pid": DEAD_PID, "pid_kind": sl.KIND_SESSION,
        "session_id": "sess-test",
    }), encoding="utf-8")
    assert sl.reapable(lock, stale_after_seconds=1800) is False


def test_reapable_default_stale_after_ignores_heartbeat_entirely(tmp_path):
    """AC6. `stale_after_seconds<=0` (the default, and every pre-#1435 call
    site) is liveness-only -- byte-for-byte unchanged even with a wildly stale
    heartbeat."""
    lock = tmp_path / ".session-lock"
    _write_modern_hb(lock, DEAD_PID, heartbeat=STALE_HEARTBEAT)
    assert sl.reapable(lock) is True                       # default arg
    assert sl.reapable(lock, stale_after_seconds=0) is True
    assert sl.reapable(lock, stale_after_seconds=-5) is True


def test_reapable_dead_but_dirty_veto_is_a_separate_concern(tmp_path):
    """AC7. `reapable()` has no notion of git working-tree cleanliness -- the
    dirty veto (#1544) lives in the two PS1 hooks, downstream of this check.
    This just documents that a stale-AND-dead lock stays REAPABLE from
    `session_lock.py`'s point of view; the hooks are what add the dirty skip."""
    lock = tmp_path / ".session-lock"
    _write_modern_hb(lock, DEAD_PID, heartbeat=STALE_HEARTBEAT)
    assert sl.reapable(lock, stale_after_seconds=1800) is True


# ─── writer refuses to clobber ───────────────────────────────────────────────


def test_write_refuses_to_clobber_another_sessions_live_lock(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id="other-session")
    try:
        sl.write_lock(lock, "sess-mine")
        raise AssertionError("expected FileExistsError")
    except FileExistsError:
        pass
    assert json.loads(lock.read_text())["session_id"] == "other-session"


def test_write_refuses_to_clobber_an_unknown_lock(tmp_path):
    # Fail-safe extends to the writer: an UNKNOWN lock might belong to a live
    # session, so taking the worktree from under it is exactly the clobber.
    lock = tmp_path / ".session-lock"
    _write_legacy(lock, DEAD_PID, session_id="other-session")
    try:
        sl.write_lock(lock, "sess-mine")
        raise AssertionError("expected FileExistsError")
    except FileExistsError:
        pass


def test_write_reclaims_a_proven_dead_lock(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, DEAD_PID, session_id="other-session")
    sl.write_lock(lock, "sess-mine", session_images={_own_image()})
    assert json.loads(lock.read_text())["session_id"] == "sess-mine"


def test_write_rewrites_my_own_lock(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_legacy(lock, DEAD_PID, session_id="sess-mine")
    written = sl.write_lock(lock, "sess-mine", session_images={_own_image()})
    assert written["pid_kind"] == sl.KIND_SESSION   # upgraded off the legacy shape


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_status_exit_codes(tmp_path):
    live, dead, unk = (tmp_path / n for n in ("a.json", "b.json", "c.json"))
    _write_modern(live, os.getpid())
    _write_modern(dead, DEAD_PID)
    _write_legacy(unk, DEAD_PID)

    r = _run("status", str(live))
    assert (r.stdout.strip(), r.returncode) == ("ALIVE", 0), r
    r = _run("status", str(dead))
    assert (r.stdout.strip(), r.returncode) == ("DEAD", 1), r
    r = _run("status", str(unk))
    assert (r.stdout.strip(), r.returncode) == ("UNKNOWN", 2), r


def test_cli_occupied_and_reapable(tmp_path):
    unk = tmp_path / ".session-lock"
    _write_legacy(unk, DEAD_PID)

    r = _run("occupied", str(unk))
    assert (r.stdout.strip(), r.returncode) == ("OCCUPIED", 0), r
    r = _run("reapable", str(unk))
    assert (r.stdout.strip(), r.returncode) == ("KEEP", 1), r


def test_cli_write_then_status(tmp_path):
    lock = tmp_path / ".session-lock"
    r = _run("write", str(lock), "--session-id", "sess-cli")
    assert r.returncode == 0, r
    payload = json.loads(r.stdout)
    assert payload["session_id"] == "sess-cli"
    assert payload["pid_kind"] in (sl.KIND_SESSION, sl.KIND_UNRESOLVED)
    assert json.loads(lock.read_text())["session_id"] == "sess-cli"


def test_cli_write_without_session_id_is_a_usage_error(tmp_path):
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), "write", str(tmp_path / ".session-lock")],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 2, r
    assert not (tmp_path / ".session-lock").exists()


def test_cli_write_ignores_a_session_id_file_under_home(tmp_path):
    """The kit takes the session id from CLAUDE_CODE_SESSION_ID only. A
    ~/.claude/.session-id file is not the kit's to read, so one planted under
    HOME must not supply an id: the write is still a usage error."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".session-id").write_text("sess-from-file\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), "write", str(tmp_path / ".session-lock")],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 2, r
    assert "CLAUDE_CODE_SESSION_ID" in r.stderr
    assert not (tmp_path / ".session-lock").exists()


def test_default_session_id_reads_the_env_var_only(tmp_path, monkeypatch):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".session-id").write_text("sess-from-file\n",
                                                      encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert sl._default_session_id() is None
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "  sess-env \n")
    assert sl._default_session_id() == "sess-env"


# ─── CLI: heartbeat + reapable --stale-after-seconds (#1435) ─────────────────


def test_cli_heartbeat_refreshes_own_lock(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id="sess-mine")
    before = json.loads(lock.read_text())

    r = _run("heartbeat", str(lock), "--session-id", "sess-mine")

    assert r.returncode == 0, r
    updated = json.loads(r.stdout)
    assert updated["heartbeat"] != before["created"]
    on_disk = json.loads(lock.read_text())
    assert on_disk["heartbeat"] == updated["heartbeat"]
    assert on_disk["session_id"] == "sess-mine"
    assert on_disk["created"] == before["created"]


def test_cli_heartbeat_refuses_a_foreign_session(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, os.getpid(), session_id="other-session")
    before = lock.read_text()

    r = _run("heartbeat", str(lock), "--session-id", "sess-mine")

    assert r.returncode == 1, r
    assert lock.read_text() == before   # unchanged


def test_cli_reapable_stale_after_seconds_flips_on_a_stale_heartbeat(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern_hb(lock, DEAD_PID, heartbeat=STALE_HEARTBEAT)

    r = _run("reapable", str(lock), "--stale-after-seconds", "1800")
    assert (r.stdout.strip(), r.returncode) == ("REAPABLE", 0), r


def test_cli_reapable_stale_after_seconds_keeps_a_fresh_heartbeat(tmp_path):
    lock = tmp_path / ".session-lock"
    _write_modern(lock, DEAD_PID, session_id="sess-test")
    sl.touch_heartbeat(lock, "sess-test")   # refresh to now

    r = _run("reapable", str(lock), "--stale-after-seconds", "1800")
    assert (r.stdout.strip(), r.returncode) == ("KEEP", 1), r


def test_cli_reapable_without_the_flag_is_unchanged(tmp_path):
    """AC6 at the CLI layer: omitting --stale-after-seconds preserves the exact
    pre-#1435 liveness-only contract, even with a wildly stale heartbeat."""
    lock = tmp_path / ".session-lock"
    _write_modern_hb(lock, DEAD_PID, heartbeat=STALE_HEARTBEAT)

    r = _run("reapable", str(lock))
    assert (r.stdout.strip(), r.returncode) == ("REAPABLE", 0), r
