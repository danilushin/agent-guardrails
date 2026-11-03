#!/usr/bin/env python3
"""session_lock.py -- session-worktree lock: writer, reader, TRI-STATE liveness (#1588).

Spec: spec-session-worktree-isolation  ADR: adr-session-worktree-isolation

WHY THIS MODULE EXISTS
----------------------
The lock-writing recipe CLAUDE.md has mandated since #1322 records
``os.getppid()`` taken from inside a Bash-tool call, on the stated premise that
the parent is "the persistent Bash-tool shell, which outlives the one-liner".
**That premise is false.** Claude Code spawns a FRESH shell for every Bash tool
call and it exits when the call returns, so the recorded PID is dead within
seconds of being written. Measured live (2026-07-13): ``os.getppid()`` returned
26252 in one tool call; in the very next call that PID was gone and
``getppid()`` returned 13688 -- a different, brand-new shell.

Two guarantees collapsed as a result:

1. **The occupation guardrail.** CLAUDE.md: "before using ANY shared worktree,
   check for a live lock ... if occupied, choose a different worktree, never
   share." Since EVERY lock reads DEAD, EVERY worktree reads *unoccupied* -- so
   a concurrent session is cleared to move into a worktree that is in active
   use. That is the silent-clobber this guard exists to prevent (it happened on
   2026-06-28: a concurrent ``git reset --hard`` wiped 13 tests mid-commit).
2. **The post-compaction re-anchor.** ``session-workspace-resolve.py`` required a
   live PID *and* a matching ``session_id``. The id always matched; the PID never
   did, so it exited non-zero for every session and the documented re-anchor
   command could never succeed.

THE FIX HAS TWO HALVES
----------------------
**(a) Record a SESSION-LIFETIME pid.** ``resolve_session_pid()`` walks the process
ancestor chain from the calling process up to the owning Claude Code session
process (the long-lived ``claude`` / ``node``) and records THAT pid, tagged
``pid_kind: "session"`` so a reader knows the pid means what it says. If no
session ancestor can be identified, the lock records ``pid: null`` +
``pid_kind: "unresolved"`` -- which reads as UNKNOWN (fail-safe) -- rather than
falling back to the transient shell's pid and lying.

**(b) Liveness becomes TRI-STATE.** The two consumers want opposite defaults, and
both are served by treating UNKNOWN as *alive*:

  - occupation guard: never hand out a worktree you cannot PROVE is free
    => UNKNOWN -> OCCUPIED;
  - GC / reaper: never reap a worktree you cannot PROVE is dead
    => UNKNOWN -> ALIVE (not reapable).

Fail-safe points the same way for both. A boolean cannot express that -- which
is why this is a new module rather than a patch in place.

BACK-COMPAT CONSTRAINT (the subtle one -- it is not visible in the code)
-----------------------------------------------------------------------
Legacy locks (``{"pid": N, "session_id": "..."}``, no ``pid_kind``) already exist
on disk. A legacy lock whose pid is DEAD must NOT be classified DEAD: two
indistinguishable histories produce it --

  * a genuinely-exited session (``hooks/session-start.ps1`` records the hook's
    parent, which IS the long-lived session process -- a correct pid), and
  * a live session whose lock was written by the broken CLAUDE.md bash recipe
    (a transient shell pid, dead on arrival).

Nothing in the file distinguishes them, so a dead legacy pid is classified
UNKNOWN => fail-safe (occupied / not reapable). Only a lock carrying
``pid_kind: "session"`` -- proof the pid was session-lifetime when written -- may
be classified confidently DEAD.

CLI
---
  session_lock.py write     <lock-path> [--session-id ID] [--force]
  session_lock.py status    <lock-path> [--session-id ID]   -> ALIVE|DEAD|UNKNOWN
  session_lock.py occupied  <lock-path> [--self-session-id ID] -> OCCUPIED|FREE
  session_lock.py heartbeat <lock-path> --session-id ID    -> refresh own heartbeat
  session_lock.py reapable  <lock-path> [--stale-after-seconds N] -> REAPABLE|KEEP

HEARTBEAT-FRESHNESS VETO (#1435, spec-reaper-heartbeat-freshness-veto)
-----------------------------------------------------------------------
Amendment 3 above named the residual gap and deferred it: "an age gate can be
added if that ever becomes a real problem (not now -- YAGNI)." #1435 is that
problem: a mis-attested False-DEAD verdict (a resumed session, same session_id,
new process, stale lock naming a dead predecessor pid) still produces a
confident, instantly-actionable DEAD with zero time component. ``heartbeat`` is
a lock field refreshed on every live prompt (P1's idempotency fast path);
``reapable(path, stale_after_seconds)`` additionally requires the heartbeat to
be older than the threshold before it will call a DEAD lock reapable. Seeding
``heartbeat`` from ``created`` means a lock that never gets refreshed (headless/
cron session, crash before the second prompt) degrades to a `created`-only age
check, not a regression below today's liveness-only gate. ``stale_after_seconds
<= 0`` (the default, and every pre-#1435 call site) preserves the exact old
behavior -- this gate is opt-in, not a new default.

SAME-PROCESS PRIOR-SESSION LOCK READS FREE (#2843)
--------------------------------------------------
``/clear`` (and a resume that switches sessions) keeps the Claude Code process
and issues a new ``CLAUDE_CODE_SESSION_ID``. The worktree's lock still names the
old id on the same, still-live session pid, so ``occupancy()`` used to read it
OCCUPIED and ``hooks/session-start.ps1`` refused to let the successor adopt its
OWN worktree: the successor ran without a lock, and the stale lock kept the
worktree un-reapable for as long as the process lived.

``occupancy()`` therefore reads FREE when the caller declares a
``self_session_id`` that differs from the lock's, the lock is
``pid_kind: "session"``, and its pid equals the caller's own resolved session
pid (also ``pid_kind: "session"``). The caller's process is alive by
definition, so the same pid under a different id means a session this process
hosted earlier, not a concurrent owner. In-process subagent lanes carry the main
session's CURRENT id (measured 2026-10-06), so a lane's lock already matched on
id before this change. ``write_lock`` compares against the pid it is about to
record, so the hook's no-``--force`` write re-keys the lock to the new id.
``liveness()`` and ``reapable()`` are unchanged.

Residual: a lane spawned BEFORE ``/clear`` that wrote a lock (old id, same pid)
would also read FREE to the successor. Measured 2026-10-06: lane worktrees carry
no ``.session-lock`` (lanes do not write locks), so the case has no writer today.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 2

# Tri-state liveness.
ALIVE = "ALIVE"
DEAD = "DEAD"
UNKNOWN = "UNKNOWN"

# pid_kind markers.
KIND_SESSION = "session"        # pid is the owning Claude Code session process
KIND_UNRESOLVED = "unresolved"  # ancestor walk found no session process; pid is null

# Image names of the long-lived Claude Code session process. `claude` is the CLI
# shim; on Windows/npm installs the session actually runs as `node`.
SESSION_IMAGES = frozenset({"claude", "claude.exe", "node", "node.exe"})

_MAX_ANCESTOR_DEPTH = 64


# ─── process-table snapshot ──────────────────────────────────────────────────


def _snapshot_windows() -> list[dict]:
    """Enumerate processes via CreateToolhelp32Snapshot (pid, ppid, image)."""
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),   # ULONG_PTR
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE,
                                         ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE,
                                        ctypes.POINTER(PROCESSENTRY32W))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    out: list[dict] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append({
                "pid": int(entry.th32ProcessID),
                "ppid": int(entry.th32ParentProcessID),
                "image": str(entry.szExeFile),
            })
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def _snapshot_proc() -> list[dict]:
    """Enumerate processes from /proc/<pid>/stat (Linux)."""
    out: list[dict] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue  # process exited between listdir and open -- normal
        # `comm` is parenthesised and may itself contain spaces/parens, so split
        # on the LAST ')': fields after it are `state ppid ...`.
        close = raw.rfind(")")
        open_ = raw.find("(")
        if close == -1 or open_ == -1 or close < open_:
            continue
        comm = raw[open_ + 1:close]
        rest = raw[close + 1:].split()
        if len(rest) < 2:
            continue
        try:
            out.append({"pid": int(name), "ppid": int(rest[1]), "image": comm})
        except ValueError:
            continue
    return out


def _snapshot_ps() -> list[dict]:
    """Enumerate processes via `ps` (POSIX hosts without /proc, e.g. macOS)."""
    try:
        res = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    out: list[dict] = []
    for line in res.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            out.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                        "image": parts[2].strip()})
        except ValueError:
            continue
    return out


def snapshot_processes() -> list[dict]:
    """Host process table as ``[{pid, ppid, image}]``. ``[]`` on any failure."""
    try:
        if os.name == "nt":
            return _snapshot_windows()
        procs = _snapshot_proc()
        return procs if procs else _snapshot_ps()
    except Exception:
        return []  # never raise: a lock writer must degrade, not crash


# ─── ancestor walk: find the owning session process ──────────────────────────


def _image_key(image: str) -> str:
    """Basename, lowercased -- `/usr/bin/node` and `node.exe` both key cleanly."""
    return os.path.basename(str(image or "").strip().replace("\\", "/")).lower()


def resolve_session_pid(start_pid: int | None = None,
                        snapshot: list[dict] | None = None,
                        session_images=SESSION_IMAGES,
                        max_depth: int = _MAX_ANCESTOR_DEPTH) -> tuple[int | None, str]:
    """Walk the ancestor chain from ``start_pid`` and return the owning Claude Code
    session process as ``(pid, "session")``.

    Returns ``(None, "unresolved")`` when no ancestor matches ``session_images``
    (or the process table is unavailable) -- fail-safe: an unresolved lock reads
    UNKNOWN, never a confident DEAD, and never a lie about a transient shell pid.

    ``snapshot`` / ``session_images`` are injectable so the walk is unit-testable
    deterministically on any host (feedback-ci-checks-cross-platform).
    """
    if start_pid is None:
        start_pid = os.getpid()
    procs = snapshot_processes() if snapshot is None else snapshot
    if not procs:
        return None, KIND_UNRESOLVED

    by_pid = {int(p["pid"]): p for p in procs if "pid" in p}
    wanted = {str(i).lower() for i in session_images}

    seen: set[int] = set()
    cur = int(start_pid)
    for _ in range(max_depth):
        if cur in seen or cur <= 0:
            break            # cycle / reached the root -- give up, fail-safe
        seen.add(cur)
        entry = by_pid.get(cur)
        if entry is None:
            break            # ancestor already exited -- chain is broken
        if _image_key(entry.get("image", "")) in wanted:
            return cur, KIND_SESSION
        cur = int(entry.get("ppid", 0) or 0)
    return None, KIND_UNRESOLVED


# ─── pid liveness (binary -- "does this pid exist right now") ────────────────


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by another user
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Non-destructive probe. NEVER use ``os.kill(pid, 0)`` here: on Windows
    CPython's ``os.kill`` calls ``TerminateProcess`` -- signal 0 would KILL the
    probed process. ``OpenProcess`` only queries."""
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access-denied means the process exists but is protected -> alive.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


# ─── lock: build / write / read ──────────────────────────────────────────────


def make_lock(session_id: str, pid: int | None = None, pid_kind: str | None = None,
              snapshot: list[dict] | None = None,
              session_images=SESSION_IMAGES) -> dict:
    """Build the lock payload for THIS process's owning session."""
    if pid_kind is None:
        pid, pid_kind = resolve_session_pid(snapshot=snapshot,
                                            session_images=session_images)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema": SCHEMA,
        "pid": pid,
        "pid_kind": pid_kind,
        "session_id": str(session_id),
        "created": created,
        # Refreshed on every live prompt by touch_heartbeat() (P1's idempotency
        # fast path, #1435) -- the second, independent signal `reapable()`'s
        # freshness veto checks on top of liveness. Seeded to `created` so a
        # lock that never gets refreshed (headless/cron session, crash before
        # the second prompt) ages identically to a bare created-only check; a
        # refreshed lock only ever gets MORE protection, never less.
        "heartbeat": created,
    }


def heartbeat_age_seconds(lock: dict) -> float | None:
    """Seconds since ``lock``'s heartbeat, falling back to ``created`` for a
    lock predating this field. ``None`` when neither is present or parses --
    callers must treat that as fail-safe-fresh (KEEP), mirroring the
    UNKNOWN-is-alive fail-safe already used for liveness."""
    raw = lock.get("heartbeat") or lock.get("created")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def touch_heartbeat(path, session_id: str) -> dict | None:
    """Refresh ``path``'s ``heartbeat`` field to now -- but ONLY when the lock's
    own ``session_id`` matches ``session_id``; a session may only bump its own
    heartbeat (the same composite-key guard as everywhere else in this module).

    Returns the updated lock dict, or ``None`` (file left untouched) when the
    lock is missing/unreadable or owned by a different session_id.
    """
    p = Path(path)
    lock = read_lock(p)
    if lock is None:
        return None
    if str(lock.get("session_id")) != str(session_id):
        return None
    lock["heartbeat"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(lock), encoding="utf-8")
    os.replace(tmp, p)   # atomic, matching write_lock's pattern
    return lock


def write_lock(path, session_id: str, force: bool = False, **kw) -> dict:
    """Write the lock at ``path``. Refuses to clobber a lock this session does not
    own and cannot prove free (occupancy is fail-safe) unless ``force``.

    Raises ``FileExistsError`` when refusing.
    """
    p = Path(path)
    # Built first so the occupancy check compares against the very pid this
    # writer is about to record (#2843 same-process exemption).
    lock = make_lock(session_id, **kw)
    if not force and p.exists():
        if occupancy(p, self_session_id=str(session_id),
                     self_pid=(lock["pid"], lock["pid_kind"])) != "FREE":
            raise FileExistsError(
                f"{p} is held by another session (or its state cannot be "
                f"proven free) -- refusing to clobber"
            )
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(lock), encoding="utf-8")
    os.replace(tmp, p)   # atomic on both platforms
    return lock


def read_lock(path) -> dict | None:
    """Parse a lock file. ``None`` for missing / unreadable / malformed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ─── tri-state liveness + the two consumer queries ───────────────────────────


def liveness(lock: dict | None, session_id: str | None = None) -> str:
    """Classify a parsed lock as ALIVE / DEAD / UNKNOWN.

    ``session_id``, when given, answers the narrower question "is this MY OWN
    live lock" -- a lock belonging to a different session is DEAD *to me*
    (the composite-key guard against Windows PID recycling, ADR Decision 2).
    Occupancy asks the opposite question and therefore does NOT pass it.
    """
    if lock is None:
        return DEAD                        # no lock == no claim
    if session_id is not None and str(lock.get("session_id")) != str(session_id):
        return DEAD                        # someone else's lock -- not mine
    kind = lock.get("pid_kind")
    if kind == KIND_UNRESOLVED:
        return UNKNOWN                     # writer could not identify the session

    pid = lock.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return DEAD                        # no usable pid at all -> no claim
    if pid_alive(pid):
        return ALIVE
    # PID is dead. Only trust that as proof when the writer proved the pid was
    # session-lifetime. A LEGACY lock (no pid_kind) may hold a transient
    # Bash-shell pid from the broken pre-#1588 recipe, which is dead the instant
    # it is written -- indistinguishable from a genuinely-exited session. Fail
    # safe (see the module docstring's back-compat block).
    if kind == KIND_SESSION:
        return DEAD
    return UNKNOWN


def lock_liveness(path, session_id: str | None = None) -> str:
    return liveness(read_lock(path), session_id)


def occupancy(path, self_session_id: str | None = None,
              self_pid: tuple[int | None, str] | None = None) -> str:
    """"Is SOMEONE ELSE in this worktree?" -> OCCUPIED | FREE.

    Fail-safe: never hand out a worktree you cannot PROVE is free, so UNKNOWN
    reads OCCUPIED. A lock carrying my OWN session_id is not someone else --
    re-entering my own worktree is always allowed.

    Nor is a session my own process hosted EARLIER (#2843): with a declared
    ``self_session_id``, a ``pid_kind: "session"`` lock whose pid is the
    caller's own session process reads FREE -- see the module docstring.
    ``self_pid`` is the caller's ``(pid, pid_kind)``; ``None`` resolves it via
    ``resolve_session_pid()`` (injectable so tests are host-independent).
    """
    lock = read_lock(path)
    if lock is None:
        return "FREE"
    if self_session_id and str(lock.get("session_id")) == str(self_session_id):
        return "FREE"
    if self_session_id and lock.get("pid_kind") == KIND_SESSION:
        lock_pid = lock.get("pid")
        if isinstance(lock_pid, int) and not isinstance(lock_pid, bool) and lock_pid > 0:
            my_pid, my_kind = resolve_session_pid() if self_pid is None else self_pid
            if my_kind == KIND_SESSION and my_pid == lock_pid:
                return "FREE"
    return "FREE" if liveness(lock) == DEAD else "OCCUPIED"


def reapable(path, stale_after_seconds: float = 0) -> bool:
    """"May GC delete this worktree?" -- only on a PROVEN-dead owner, and, when
    ``stale_after_seconds > 0``, only once its heartbeat is ALSO stale (#1435
    heartbeat-freshness veto layered on top of liveness).

    Fail-safe throughout: never reap what you cannot prove is dead (UNKNOWN ->
    False); and with a threshold given, never reap a lock whose staleness
    cannot be proven either (``heartbeat_age_seconds`` -> None -> KEEP).

    ``stale_after_seconds <= 0`` (the default) is the pre-#1435 liveness-only
    behavior, byte-for-byte unchanged -- the freshness gate is opt-in.
    """
    lock = read_lock(path)
    if liveness(lock) != DEAD:
        return False
    if stale_after_seconds <= 0 or lock is None:
        return True
    age = heartbeat_age_seconds(lock)
    if age is None:
        return False   # can't prove staleness -- fail-safe KEEP
    return age > stale_after_seconds


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _default_session_id() -> str | None:
    """The calling session's id from CLAUDE_CODE_SESSION_ID, else None (unknown).

    The kit reads the variable only: a file under the home directory would be a
    convention of some other setup, not something Claude Code writes."""
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    return sid or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Session worktree lock (#1588).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="write a lock naming this session's process")
    w.add_argument("lock_path")
    w.add_argument("--session-id", default=None,
                   help="default: $CLAUDE_CODE_SESSION_ID")
    w.add_argument("--force", action="store_true",
                   help="overwrite even a lock held by another session")

    s = sub.add_parser("status", help="ALIVE | DEAD | UNKNOWN")
    s.add_argument("lock_path")
    s.add_argument("--session-id", default=None,
                   help="require the lock's session_id to equal this value")

    o = sub.add_parser("occupied", help="OCCUPIED | FREE (UNKNOWN reads OCCUPIED)")
    o.add_argument("lock_path")
    o.add_argument("--self-session-id", default=None,
                   help="my own session id -- my own lock never reads OCCUPIED")

    h = sub.add_parser("heartbeat", help="refresh this lock's own heartbeat (#1435)")
    h.add_argument("lock_path")
    h.add_argument("--session-id", required=True,
                   help="must equal the lock's own session_id or the refresh is refused")

    r = sub.add_parser("reapable", help="REAPABLE | KEEP (UNKNOWN reads KEEP)")
    r.add_argument("lock_path")
    r.add_argument("--stale-after-seconds", type=float, default=0,
                   help="also require heartbeat age > N seconds (#1435). "
                        "0 or omitted = liveness-only, pre-#1435 behavior")

    args = ap.parse_args(argv)

    if args.cmd == "write":
        session_id = args.session_id or _default_session_id()
        if not session_id:
            print("error: no session-id (pass --session-id or set "
                  "CLAUDE_CODE_SESSION_ID)", file=sys.stderr)
            return 2
        try:
            lock = write_lock(args.lock_path, session_id, force=args.force)
        except FileExistsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"error: cannot write {args.lock_path}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(lock))
        if lock["pid_kind"] == KIND_UNRESOLVED:
            # Not a failure: the lock is valid and fail-safe (reads UNKNOWN ->
            # occupied / not reapable). Say so, since the guard is now weaker.
            print("warning: no Claude Code session process found in the ancestor "
                  "chain -- lock records pid=null (reads UNKNOWN, fail-safe)",
                  file=sys.stderr)
        return 0

    if args.cmd == "status":
        state = lock_liveness(args.lock_path, args.session_id)
        print(state)
        return {ALIVE: 0, DEAD: 1, UNKNOWN: 2}[state]

    if args.cmd == "occupied":
        state = occupancy(args.lock_path, args.self_session_id)
        print(state)
        return 0 if state == "OCCUPIED" else 1

    if args.cmd == "heartbeat":
        updated = touch_heartbeat(args.lock_path, args.session_id)
        if updated is None:
            print("error: lock missing/unreadable, or owned by a different "
                  "session_id -- refusing to touch it", file=sys.stderr)
            return 1
        print(json.dumps(updated))
        return 0

    if args.cmd == "reapable":
        ok = reapable(args.lock_path, args.stale_after_seconds)
        print("REAPABLE" if ok else "KEEP")
        return 0 if ok else 1

    return 2  # unreachable: argparse requires a subcommand


if __name__ == "__main__":
    sys.exit(main())
