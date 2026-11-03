#!/usr/bin/env python3
"""merge_lock.py -- a crash-safe merge mutex serializing merges to ``main`` (#1097).

Item 2 of 3 from #1063 (board convening 2026-06-24, verdict GO). Parallel
``eng-flow --auto-merge`` runs contaminate each other's review/merge (wrong diff,
mangled merge args) -- the documented ``feedback-engflow-concurrent-automerge-unsafe``
(#500) failure. dup_scan's ``--prebranch`` HOLD is advisory and fires only at
BRANCH time; it cannot *hold* anything across the minutes-long merge window. This
turns that advisory into a real lock: only one merger to ``main`` at a time.

Lock location -- the lock file lives in the repo's **git common dir**
(``git rev-parse --git-common-dir``), which is SHARED by every worktree of the
repo on this machine, so all concurrent eng-flow runs (each in its own worktree)
contend on the *same* lock. Falls back to the system temp dir if git is
unavailable.

Crash safety -- the lock carries an ``acquired_at`` wall-clock stamp. ``acquire``
treats a lock older than ``--ttl`` seconds as STALE and reclaims it, so a run that
died mid-merge (kill -9, crash, power loss) never wedges the fleet: the next
merger breaks the stale lock after the TTL. ``release`` is idempotent and
owner-checked (it never removes another holder's live lock).

Queueing -- ``acquire --wait N`` polls up to N seconds for a live lock to free
(serialize/queue) before giving up; ``--wait 0`` (default) returns immediately.

Leases (#3237) -- a lock may also name the PR it protects (``pr``, ``repo``) and
carry its own ``expires_at``. ``premerge_guard --resync`` takes one before its
update-branch and keeps it past the lap's GO, so a sibling session's merge cannot
land inside the ~30-min lap and re-stale it (2 of #3228's 4 laps on 2026-10-22).
Three rules apply only to a lock that carries ``pr``; a plain lock (eng-flow,
merge_go) behaves exactly as before:
  - its own ``expires_at`` decides staleness, so a caller's shorter ``--ttl``
    cannot break a live lease;
  - it is RE-ENTRANT for its own PR -- ``pr=`` passed, or parsed from a
    ``...:pr-<N>:...`` holder (merge_go's format, so ``merge_go <N>`` still merges
    the PR that holds the lease); the same number in another repo is not the PR;
  - it is SPENT once its PR is no longer OPEN, and is broken. The Mgo path merges
    with a bare ``gh pr merge`` that never releases, so this is its release. The
    probe (a REST ``pulls/<N>`` call) runs at most once per
    ``SPENT_PROBE_INTERVAL`` while queueing; no answer counts as live, which
    ``expires_at`` bounds.

Exit codes (mirror dup_scan.py / premerge_guard.py): 0 = GO (acquired / released),
3 = HOLD (a live lock is held by another run and did not free within --wait),
2 = UNDETERMINED (a filesystem probe failed -> fail-open; the caller proceeds, as
a best-effort serializer must never wedge the fleet harder than no lock at all).

Usage:
  merge_lock.py acquire [--holder ID] [--ttl 1800] [--wait 0] [--lock-file P] [--json]
  merge_lock.py release [--holder ID] [--lock-file P] [--json]
  merge_lock.py status  [--ttl 1800] [--lock-file P] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GO = 0            # acquired / released
HOLD = 3          # a live lock is held by another run (mirrors dup_scan PREBRANCH_HOLD)
UNDETERMINED = 2  # filesystem probe failed -> fail-open, never wedges the fleet

DEFAULT_TTL = 1800       # 30 min: > the longest legitimate hold (CI green-wait + merge)
_POLL_INTERVAL = 5.0     # seconds between queue polls
_LOCK_BASENAME = "eng-flow-merge-main.lock"
SPENT_PROBE_INTERVAL = 60.0   # seconds between "is the lease's PR still OPEN?" probes
_PR_STATE_TIMEOUT_S = 15
_HOLDER_PR_RE = re.compile(r"(?:^|:)pr-(\d+)(?::|$)")


def default_lock_path():
    """The shared-across-worktrees lock path: ``<git-common-dir>/<basename>``.

    ``--git-common-dir`` resolves to the ONE real ``.git`` dir even from a linked
    worktree, so every worktree of the repo contends on the same lock. Falls back
    to the system temp dir when git is unavailable (still a single fixed path)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            common = Path(out.stdout.strip())
            if not common.is_absolute():
                common = Path.cwd() / common
            return str(common / _LOCK_BASENAME)
    except (OSError, ValueError):
        pass
    return str(Path(tempfile.gettempdir()) / _LOCK_BASENAME)


def _read_lock(lock_file):
    """Parse the lock file -> dict, or None if missing / unreadable / corrupt."""
    try:
        with open(lock_file, "r", encoding="utf-8") as fh:
            return json.loads(fh.read())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _break_lock(lock_file):
    """Best-effort remove of a stale/corrupt lock. True if gone afterwards."""
    try:
        os.unlink(lock_file)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _lock_age(info, clock):
    """Seconds since the lock was acquired, or None if unstamped/unparseable."""
    if not info or "acquired_at" not in info:
        return None
    try:
        return clock() - float(info["acquired_at"])
    except (TypeError, ValueError):
        return None


def _expires_in(info, clock):
    """Seconds left on a lease's own ``expires_at``, or None when it carries none."""
    if not info or "expires_at" not in info:
        return None
    try:
        return float(info["expires_at"]) - clock()
    except (TypeError, ValueError):
        return None


def _is_stale(info, ttl, clock):
    """A lease's own expiry wins over the caller's ttl (#3237); a plain lock ages out."""
    left = _expires_in(info, clock)
    if left is not None:
        return left < 0
    age = _lock_age(info, clock)
    return age is not None and age > ttl


def _as_pr(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def holder_pr(holder):
    """The PR a ``...:pr-<N>:...`` holder string names (merge_go's format), or None."""
    m = _HOLDER_PR_RE.search(holder or "")
    return int(m.group(1)) if m else None


def _same_repo(a, b):
    """Unknown on either side matches on the PR number alone."""
    return not a or not b or a.lower() == b.lower()


def gh_pr_state(repo, pr):
    """'open' / 'closed' from one REST call, or None when it cannot be read."""
    try:
        out = subprocess.run(["gh", "api", "repos/%s/pulls/%d" % (repo, pr), "--jq", ".state"],
                             capture_output=True, text=True, timeout=_PR_STATE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    state = (out.stdout or "").strip().lower()
    return state if out.returncode == 0 and state else None


def _replace_lock(lock_file, payload):
    """Atomic overwrite (temp + os.replace) for a same-PR re-entry. True on success."""
    data = payload.encode("utf-8")
    tmp = "%s.%d.tmp" % (lock_file, os.getpid())
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, lock_file)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def acquire(lock_file, holder, ttl=DEFAULT_TTL, wait=0.0,
            sleep=time.sleep, now=time.monotonic, clock=time.time,
            pr=None, repo=None, expires_in=None, pr_state=None):
    """Acquire the merge lock. Returns one of 'acquired' | 'held' | 'error'.

    - Atomic ``O_CREAT|O_EXCL`` create => 'acquired'.
    - Lock present + stale (age > ttl, or a lease past its own expiry) => break
      it and retry => 'acquired'.
    - Lock present + corrupt/unreadable => break it and retry (bounded).
    - A live lease on this caller's own PR => taken over => 'acquired' (#3237).
    - A live lease whose PR ``pr_state`` reads as not OPEN => spent: broken and
      retried (#3237).
    - Lock present + live => poll up to ``wait`` seconds; still held at the
      deadline => 'held'.
    - A filesystem error that prevents progress => 'error' (caller fails open).

    ``pr``/``repo``/``expires_in`` make the lock a lease; ``pr_state(repo, pr)``
    is the spent probe (default: one REST call through gh).
    """
    deadline = now() + max(0.0, wait)
    corrupt_breaks = 0
    my_pr = _as_pr(pr) if pr is not None else holder_pr(holder)
    probe = pr_state or gh_pr_state
    last_probe = None

    def _payload():
        body = {"holder": holder, "acquired_at": clock(), "pid": os.getpid()}
        if pr is not None:
            body.update({"pr": _as_pr(pr), "repo": repo})
        if expires_in is not None:
            body["expires_at"] = clock() + float(expires_in)
        return json.dumps(body)

    while True:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, _payload().encode("utf-8"))
            finally:
                os.close(fd)
            return "acquired"
        except FileExistsError:
            pass
        except PermissionError:
            # Windows raises PermissionError on a delete-pending name, not only
            # FileExistsError (feedback-windows-exclusive-create-lock-race). Treat
            # as "exists" and fall through to the staleness check.
            pass
        except OSError:
            return "error"

        info = _read_lock(lock_file)
        if info is not None and _is_stale(info, ttl, clock):
            if not _break_lock(lock_file):
                return "error"
            continue  # stale broken -> retry the create
        if info is None:
            if not os.path.exists(lock_file):
                # Vanished between the failed create and the read (released, or a
                # delete-pending flicker) -> retry the create immediately.
                continue
            # Exists but unparseable. A real holder writes valid JSON atomically
            # right after O_EXCL create, so a persistently-corrupt file is dead
            # garbage, not a live lock -- break it. One re-read first guards the
            # microsecond mid-write window. This integrity recovery is NOT bounded
            # by the queue deadline (a corrupt lock must never wedge acquisition).
            corrupt_breaks += 1
            if corrupt_breaks >= 2:
                if not _break_lock(lock_file):
                    return "error"
                corrupt_breaks = 0
            continue
        # live lock held by another run
        lease_pr = _as_pr(info.get("pr"))
        if lease_pr is not None:
            if my_pr == lease_pr and _same_repo(repo, info.get("repo")):
                return "acquired" if _replace_lock(lock_file, _payload()) else "error"
            if info.get("repo") and (last_probe is None
                                     or now() - last_probe >= SPENT_PROBE_INTERVAL):
                last_probe = now()
                state = probe(info["repo"], lease_pr)
                if state is not None and state != "open":
                    if not _break_lock(lock_file):
                        return "error"
                    continue  # spent lease broken -> retry the create
        if now() >= deadline:
            return "held"
        sleep(min(_POLL_INTERVAL, max(0.1, deadline - now())))


def release(lock_file, holder=None):
    """Release the lock. Returns 'released' | 'not_owner' | 'error'.

    Idempotent (a missing lock is 'released'). Owner-checked: with a ``holder``
    given, refuses to remove a lock stamped with a *different* holder."""
    info = _read_lock(lock_file)
    if info is None:
        return "released"  # already gone / unreadable -> nothing of ours to hold
    if holder is not None:
        owner = info.get("holder")
        if owner is not None and owner != holder:
            return "not_owner"
    try:
        os.unlink(lock_file)
    except FileNotFoundError:
        pass
    except OSError:
        return "error"
    return "released"


def status(lock_file, ttl=DEFAULT_TTL, clock=time.time):
    """Report lock state: {'state': 'free'|'held'|'stale', 'holder', 'age'}, plus
    'pr', 'repo' and 'expires_in' for a lease (#3237)."""
    info = _read_lock(lock_file)
    if info is None:
        return {"state": "free"}
    st = {"state": "stale" if _is_stale(info, ttl, clock) else "held",
          "holder": info.get("holder"), "age": _lock_age(info, clock)}
    if _as_pr(info.get("pr")) is not None:
        st.update({"pr": _as_pr(info.get("pr")), "repo": info.get("repo"),
                   "expires_in": _expires_in(info, clock)})
    return st


def _default_holder():
    return os.environ.get("MERGE_LOCK_HOLDER") or ("pid-%d" % os.getpid())


def main(argv=None):
    ap = argparse.ArgumentParser(description="Crash-safe merge mutex for main (#1097).")
    ap.add_argument("action", choices=["acquire", "release", "status"])
    ap.add_argument("--holder", default=None, help="lock owner id (default: pid / $MERGE_LOCK_HOLDER)")
    ap.add_argument("--ttl", type=float, default=DEFAULT_TTL, help="stale-lock TTL seconds (default 1800)")
    ap.add_argument("--wait", type=float, default=0.0, help="acquire: seconds to queue for a live lock")
    ap.add_argument("--lock-file", default=None, help="lock path (default: <git-common-dir>/%s)" % _LOCK_BASENAME)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    lock_file = args.lock_file or os.environ.get("MERGE_LOCK_FILE") or default_lock_path()
    holder = args.holder or _default_holder()

    if args.action == "acquire":
        try:
            res = acquire(lock_file, holder, ttl=args.ttl, wait=args.wait)
        except Exception as exc:  # noqa: BLE001 - any unexpected failure is fail-open
            if args.json:
                print(json.dumps({"action": "acquire", "result": "error", "error": str(exc)}))
            else:
                print("UNDETERMINED -- merge-lock acquire failed (%s); NOT holding the "
                      "fleet (fail-open)." % exc, file=sys.stderr)
            return UNDETERMINED
        code = {"acquired": GO, "held": HOLD, "error": UNDETERMINED}[res]
        if args.json:
            print(json.dumps({"action": "acquire", "result": res, "lock_file": lock_file,
                              "holder": holder, "exit_code": code}))
        elif res == "acquired":
            print("GO -- merge lock acquired (%s)." % holder)
        elif res == "held":
            st = status(lock_file, ttl=args.ttl)
            print("HOLD -- another merge to main is in progress (holder=%s, age=%ss); "
                  "did not free within --wait %ss. Retry shortly."
                  % (st.get("holder"), st.get("age"), args.wait))
        else:
            print("UNDETERMINED -- merge-lock acquire could not make progress; "
                  "proceeding without the lock (fail-open).", file=sys.stderr)
        return code

    if args.action == "release":
        res = release(lock_file, holder=holder)
        code = GO if res in ("released", "not_owner") else UNDETERMINED
        if args.json:
            print(json.dumps({"action": "release", "result": res, "lock_file": lock_file,
                              "holder": holder, "exit_code": code}))
        else:
            print("released merge lock" if res == "released"
                  else "merge lock not owned by %s -- left in place" % holder if res == "not_owner"
                  else "UNDETERMINED -- could not release merge lock")
        return code

    # status
    st = status(lock_file, ttl=args.ttl)
    code = HOLD if st["state"] == "held" else GO
    if args.json:
        st.update({"lock_file": lock_file, "exit_code": code})
        print(json.dumps(st))
    else:
        print("merge lock: %s%s" % (st["state"],
              "" if st["state"] == "free" else " (holder=%s, age=%ss)" % (st.get("holder"), st.get("age"))))
    return code


if __name__ == "__main__":
    sys.exit(main())
