"""The one-shot merge authorization that merge_gate.py consumes.

phrase_arm.py writes it when a person types `mgo <N>` as their entire prompt and the
pre-merge check returns GO. merge_gate.py reads it and deletes it on the one merge it
allows. One file per PR, `cc-merge-auth.<N>.json`, valid for MARKER_TTL_MINUTES. Two
sessions authorizing two different PRs inside the TTL never share a file, so neither
can clobber or refuse the other.

Both sides take the directory from marker_dir() below: `$CLAUDE_PLUGIN_DATA`. Claude
Code exports it to every hook process of the plugin and keeps it across plugin
updates. The plugin's own directory (`$CLAUDE_PLUGIN_ROOT`) moves on every update,
and a directory derived from the user's repo or cwd can differ between the prompt
hook and the tool hook, so neither is used. With CLAUDE_PLUGIN_DATA unset there is
no directory at all: arm() refuses and the gate finds no marker, so both fail closed.

Every arm appends its record to `arm-log.jsonl` in the same directory. When the
plugin's notify_command option is set, that command also runs on each arm, split
like a shell command line but run without a shell, with the record as JSON on stdin.
Both are best-effort: a failure there is reported on stderr and never stops an arm.

Residual: nothing at the OS level stops a process that can run shell commands from
writing a marker file itself. The arm log and notify_command make a phrase arm
visible; they cannot see a marker written by hand.
"""
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone

MARKER_PREFIX = "cc-merge-auth"

# A target is a PR number (the gate reads at most 7 digits). It becomes part of a
# file name, so anything else -- a separator, `..` -- is refused before it can reach
# the filesystem. There is no push target: no marker clears a push to main.
_TARGET_RE = re.compile(r"\d{1,7}")

# The marker's validity window. merge_gate.py reads this constant, so the window the
# gate enforces and the one the arm reports cannot drift apart.
MARKER_TTL_MINUTES = 10

DATA_DIR_ENV = "CLAUDE_PLUGIN_DATA"
NOTIFY_ENV = "CLAUDE_PLUGIN_OPTION_NOTIFY_COMMAND"
ARM_LOG = "arm-log.jsonl"

# The notifier runs inside the UserPromptSubmit hook, after the pre-merge check, so
# its budget is part of that hook's timeout (hooks.json; pinned by a test).
NOTIFY_TIMEOUT_S = 5


class NoMarkerDir(RuntimeError):
    """CLAUDE_PLUGIN_DATA is not set, so there is no directory both hooks agree on."""


def marker_dir():
    """`$CLAUDE_PLUGIN_DATA`, or None when Claude Code did not set it."""
    return os.environ.get(DATA_DIR_ENV) or None


def marker_name(target):
    """`cc-merge-auth.<target>.json`. Raises ValueError for anything but a PR number."""
    t = str(target)
    if not _TARGET_RE.fullmatch(t):
        raise ValueError("not a merge-auth target: %r (a PR number of up to 7 digits)"
                         % (target,))
    return "%s.%s.json" % (MARKER_PREFIX, t)


def marker_path(target):
    """Absolute path of `target`'s marker. Raises NoMarkerDir without CLAUDE_PLUGIN_DATA."""
    directory = marker_dir()
    if not directory:
        raise NoMarkerDir("%s is not set" % DATA_DIR_ENV)
    return os.path.join(directory, marker_name(target))


def arm(target, note, session_id=""):
    """Write the one-shot marker for `target`, record the arm, and return the payload.

    The note is the caller's verbatim text: phrase_arm.py passes the prompt it read
    from its own stdin, so no agent-written text reaches this function. Re-arming the
    same target overwrites its marker; another target's marker is never touched.
    """
    path = marker_path(target)   # validates the target and the directory first
    payload = {
        "target": str(target),
        "ts": datetime.now(timezone.utc).isoformat(),
        "note": note.strip(),
        "session_id": session_id,
    }
    # Encode first, then temp + replace: the gate must never read a half-written
    # marker (an `open(path, "w")` truncates before the payload lands).
    data = json.dumps(payload, indent=2).encode("utf-8")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    _record_arm(payload)
    return payload


def _warn(message):
    sys.stderr.write("agent-guardrails: %s\n" % message)


def _record_arm(payload):
    """Append the arm to the log and run notify_command. Never raises."""
    line = (json.dumps(payload) + "\n").encode("utf-8")
    try:
        fd = os.open(os.path.join(marker_dir(), ARM_LOG),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as exc:
        _warn("arm log not written: %s" % (exc,))

    command = (os.environ.get(NOTIFY_ENV) or "").strip()
    if not command:
        return
    try:
        argv = shlex.split(command)
        proc = subprocess.run(argv, input=json.dumps(payload).encode("utf-8"),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=NOTIFY_TIMEOUT_S)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        _warn("notify_command failed: %s" % (exc,))
        return
    if proc.returncode != 0:
        _warn("notify_command exited %d" % proc.returncode)
