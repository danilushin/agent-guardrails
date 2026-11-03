#!/usr/bin/env python3
"""Tests for merge_lock.py (#1097) -- the crash-safe merge mutex.

Run: python -m pytest tests/test_merge_lock.py -q
(NEVER `python test_merge_lock.py` -- a pytest file run directly exits 0 doing
nothing; feedback-pytest-file-false-pass-direct-run.)
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_ML = _HERE.parents[1] / "scripts" / "merge_lock.py"

spec = importlib.util.spec_from_file_location("merge_lock", _ML)
ml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ml)


# --- a deterministic fake clock + sleep so wait-loops never actually block ---
class FakeClock:
    """Monotonic-ish fake: each call to now() advances by `step`. sleep() also
    advances it, so a poll loop with a deadline terminates deterministically."""

    def __init__(self, start=1000.0, step=1.0):
        self.t = start
        self.step = step

    def now(self):
        self.t += self.step
        return self.t

    def sleep(self, secs):
        self.t += max(0.0, secs)


def _write_lock(path, holder="other", acquired_at=1000.0):
    path.write_text(json.dumps({"holder": holder, "acquired_at": acquired_at,
                                "pid": 999}), encoding="utf-8")


# ----------------------------- acquire: free ------------------------------
def test_acquire_on_free_lock_succeeds(tmp_path):
    lf = tmp_path / "m.lock"
    assert ml.acquire(str(lf), "me") == "acquired"
    assert lf.exists()
    info = json.loads(lf.read_text(encoding="utf-8"))
    assert info["holder"] == "me"
    assert "acquired_at" in info


# ----------------------- acquire: live lock held (DoD) --------------------
def test_second_merge_blocks_while_first_holds(tmp_path):
    """DoD #1097: a 2nd concurrent merge is BLOCKED while the 1st holds."""
    lf = tmp_path / "m.lock"
    # first holder takes the lock fresh (recent timestamp)
    import time
    _write_lock(lf, holder="first", acquired_at=time.time())
    # second merger, no wait -> HOLD (not acquired)
    assert ml.acquire(str(lf), "second", ttl=ml.DEFAULT_TTL, wait=0) == "held"
    # lock still owned by first
    assert json.loads(lf.read_text(encoding="utf-8"))["holder"] == "first"


def test_held_lock_with_wait_gives_up_after_deadline(tmp_path):
    """A live lock that never frees: --wait polls then returns 'held'. The fake
    clock guarantees the deadline is crossed without real sleeping."""
    lf = tmp_path / "m.lock"
    import time
    _write_lock(lf, holder="first", acquired_at=time.time())
    fc = FakeClock(step=1.0)
    res = ml.acquire(str(lf), "second", ttl=ml.DEFAULT_TTL, wait=3,
                     sleep=fc.sleep, now=fc.now)
    assert res == "held"


# ---------------------- acquire: stale lock reclaim (DoD) -----------------
def test_stale_lock_is_reclaimed(tmp_path):
    """DoD #1097: a stale lock (age > ttl) is reclaimable -- crash safety."""
    lf = tmp_path / "m.lock"
    import time
    # lock acquired 10000s ago, ttl 1s -> stale
    _write_lock(lf, holder="dead-run", acquired_at=time.time() - 10000)
    res = ml.acquire(str(lf), "me", ttl=1, wait=0)
    assert res == "acquired"
    assert json.loads(lf.read_text(encoding="utf-8"))["holder"] == "me"


# ----------------------------- release ------------------------------------
def test_release_removes_owned_lock(tmp_path):
    lf = tmp_path / "m.lock"
    ml.acquire(str(lf), "me")
    assert ml.release(str(lf), holder="me") == "released"
    assert not lf.exists()


def test_release_is_idempotent_when_missing(tmp_path):
    lf = tmp_path / "m.lock"
    assert ml.release(str(lf), holder="me") == "released"  # nothing there


def test_release_refuses_other_holders_lock(tmp_path):
    lf = tmp_path / "m.lock"
    _write_lock(lf, holder="someone-else", acquired_at=__import__("time").time())
    assert ml.release(str(lf), holder="me") == "not_owner"
    assert lf.exists()  # left in place -- never steal a live lock


# ----------------------------- status -------------------------------------
def test_status_free(tmp_path):
    lf = tmp_path / "m.lock"
    assert ml.status(str(lf))["state"] == "free"


def test_status_held_then_stale(tmp_path):
    lf = tmp_path / "m.lock"
    import time
    _write_lock(lf, holder="x", acquired_at=time.time())
    assert ml.status(str(lf), ttl=ml.DEFAULT_TTL)["state"] == "held"
    _write_lock(lf, holder="x", acquired_at=time.time() - 10000)
    assert ml.status(str(lf), ttl=1)["state"] == "stale"


# ----------------------------- corrupt lock -------------------------------
def test_corrupt_lock_is_broken_and_acquired(tmp_path):
    """An unparseable lock file must not wedge acquisition forever."""
    lf = tmp_path / "m.lock"
    lf.write_text("not-json{{{", encoding="utf-8")
    res = ml.acquire(str(lf), "me", ttl=ml.DEFAULT_TTL, wait=0)
    assert res == "acquired"
    assert json.loads(lf.read_text(encoding="utf-8"))["holder"] == "me"


# ----------------------------- CLI exit codes -----------------------------
def test_cli_acquire_release_exit_codes(tmp_path, capsys):
    lf = str(tmp_path / "m.lock")
    assert ml.main(["acquire", "--lock-file", lf, "--holder", "me"]) == ml.GO
    # second acquire with no wait -> HOLD exit 3
    assert ml.main(["acquire", "--lock-file", lf, "--holder", "other", "--wait", "0"]) == ml.HOLD
    # release by owner -> GO
    assert ml.main(["release", "--lock-file", lf, "--holder", "me"]) == ml.GO


def test_cli_status_exit_codes(tmp_path):
    lf = str(tmp_path / "m.lock")
    assert ml.main(["status", "--lock-file", lf]) == ml.GO  # free
    ml.main(["acquire", "--lock-file", lf, "--holder", "me"])
    assert ml.main(["status", "--lock-file", lf]) == ml.HOLD  # held


def test_cli_json_output(tmp_path, capsys):
    lf = str(tmp_path / "m.lock")
    ml.main(["acquire", "--lock-file", lf, "--holder", "me", "--json"])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["result"] == "acquired" and out["exit_code"] == ml.GO


# ------------------------- leases (#3237) ---------------------------------
# A lease is a lock that names the PR it protects and carries its own expiry:
# premerge_guard --resync holds one from its update-branch until the PR merges,
# so a sibling session's merge cannot land inside the ~30-min lap. Plain locks
# (eng-flow, merge_go) carry no `pr` and must behave exactly as before.
import time as _time


def _write_lease(path, pr=3228, repo="o/r", holder="premerge_guard:resync:pr-3228:pid-1",
                 acquired_at=None, expires_in=600.0):
    now = _time.time()
    path.write_text(json.dumps({
        "holder": holder, "acquired_at": now if acquired_at is None else acquired_at,
        "pid": 999, "pr": pr, "repo": repo, "expires_at": now + expires_in,
    }), encoding="utf-8")


def _never_probe(repo, pr):
    raise AssertionError("pr_state probed for %s#%s" % (repo, pr))


def test_acquire_writes_the_lease_fields(tmp_path):
    lf = tmp_path / "m.lock"
    before = _time.time()
    assert ml.acquire(str(lf), "me", pr=3228, repo="o/r", expires_in=900) == "acquired"
    info = json.loads(lf.read_text(encoding="utf-8"))
    assert info["pr"] == 3228 and info["repo"] == "o/r"
    assert before + 900 <= info["expires_at"] <= _time.time() + 900


def test_a_lease_own_expiry_beats_a_shorter_caller_ttl(tmp_path):
    """eng-flow judges locks at --ttl 1800; a lap alone can take 30 min. A live
    lease must not be broken by a caller whose ttl is shorter than its life."""
    lf = tmp_path / "m.lock"
    _write_lease(lf, acquired_at=_time.time() - 10000, expires_in=600)
    assert ml.acquire(str(lf), "eng-flow-run", ttl=1, wait=0,
                      pr_state=lambda r, p: "open") == "held"
    assert ml.status(str(lf), ttl=1)["state"] == "held"


def test_a_lease_past_its_own_expiry_is_reclaimed(tmp_path):
    lf = tmp_path / "m.lock"
    _write_lease(lf, expires_in=-1)
    assert ml.acquire(str(lf), "me", wait=0, pr_state=_never_probe) == "acquired"
    assert ml.status(str(lf))["holder"] == "me"


def test_the_same_pr_re_enters_its_own_lease(tmp_path):
    """A second lap on the PR that holds the lease (or its GO-time refresh) takes
    it over instead of queueing behind itself."""
    lf = tmp_path / "m.lock"
    _write_lease(lf, pr=3228)
    assert ml.acquire(str(lf), "premerge_guard:resync:pr-3228:pid-2", pr=3228,
                      repo="o/r", expires_in=60, pr_state=_never_probe) == "acquired"
    info = json.loads(lf.read_text(encoding="utf-8"))
    assert info["holder"] == "premerge_guard:resync:pr-3228:pid-2"
    assert info["expires_at"] <= _time.time() + 60


def test_merge_go_re_enters_the_lease_of_the_pr_it_merges(tmp_path):
    """hooks/merge_go.py passes no pr (a hook edit is gated), but its holder is
    `merge_go:pr-<N>:pid-<P>` -- `! merge_go 3228` must not queue behind 3228's own lease."""
    lf = tmp_path / "m.lock"
    _write_lease(lf, pr=3228)
    assert ml.acquire(str(lf), "merge_go:pr-3228:pid-77", wait=0,
                      pr_state=_never_probe) == "acquired"
    assert ml.status(str(lf))["holder"] == "merge_go:pr-3228:pid-77"


def test_another_pr_is_held_by_a_live_lease_on_an_open_pr(tmp_path):
    lf = tmp_path / "m.lock"
    _write_lease(lf, pr=3228)
    assert ml.acquire(str(lf), "merge_go:pr-3240:pid-77", wait=0,
                      pr_state=lambda r, p: "open") == "held"
    assert ml.status(str(lf))["pr"] == 3228


def test_the_same_number_in_another_repo_is_not_the_same_lease(tmp_path):
    lf = tmp_path / "m.lock"
    _write_lease(lf, pr=3228, repo="o/r")
    assert ml.acquire(str(lf), "x", pr=3228, repo="o/other", wait=0,
                      pr_state=lambda r, p: "open") == "held"


def test_a_spent_lease_is_broken(tmp_path):
    """The lease is released by its PR leaving OPEN: the Mgo path merges with a
    bare `gh pr merge` that never calls release."""
    lf = tmp_path / "m.lock"
    _write_lease(lf, pr=3228)
    seen = []
    assert ml.acquire(str(lf), "merge_go:pr-3240:pid-77", wait=0,
                      pr_state=lambda r, p: seen.append((r, p)) or "closed") == "acquired"
    assert seen == [("o/r", 3228)]


def test_an_unprobeable_lease_counts_as_live(tmp_path):
    """No answer is not 'spent': the lease stays until its own expiry bounds it."""
    lf = tmp_path / "m.lock"
    _write_lease(lf, pr=3228)
    assert ml.acquire(str(lf), "other", wait=0, pr_state=lambda r, p: None) == "held"


def test_the_spent_probe_is_rate_limited_while_queueing(tmp_path):
    """eng-flow queues up to 1800 s at a 5 s poll; one REST call per poll would be
    360 calls. The probe runs at most once per SPENT_PROBE_INTERVAL."""
    lf = tmp_path / "m.lock"
    _write_lease(lf, pr=3228, expires_in=100000)
    calls = []
    fc = FakeClock(step=1.0)
    res = ml.acquire(str(lf), "other", wait=600, sleep=fc.sleep, now=fc.now,
                     pr_state=lambda r, p: calls.append(p) or "open")
    assert res == "held"
    assert 1 <= len(calls) <= 600 / ml.SPENT_PROBE_INTERVAL + 1


def test_a_plain_lock_never_probes_a_pr(tmp_path):
    lf = tmp_path / "m.lock"
    _write_lock(lf, holder="merge_go:pr-3228:pid-1", acquired_at=_time.time())
    assert ml.acquire(str(lf), "other", wait=0, pr_state=_never_probe) == "held"


def test_status_reports_the_lease(tmp_path):
    lf = tmp_path / "m.lock"
    _write_lease(lf, pr=3228, repo="o/r", expires_in=300)
    st = ml.status(str(lf))
    assert (st["state"], st["pr"], st["repo"]) == ("held", 3228, "o/r")
    assert 0 < st["expires_in"] <= 300


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
