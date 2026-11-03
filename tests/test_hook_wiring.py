"""hooks/hooks.json and hooks/run-py: the hooks are registered, launched on a Python
that can run them, and the two halves of the merge authorization meet.

A hook that is never registered never runs, and its unit tests stay green the whole
time, so the registration is pinned here. The end-to-end cases launch the gate the way
hooks.json does -- run-py with its arguments, `${CLAUDE_PLUGIN_ROOT}` substituted --
and give it CLAUDE_PLUGIN_DATA the way Claude Code does for plugin hooks.
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1]
HOOKS = KIT / "hooks"
RUN_PY = HOOKS / "run-py"
sys.path.insert(0, str(HOOKS))
import merge_marker  # noqa: E402
import phrase_arm  # noqa: E402

CONFIG = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))


def _entries(event):
    return [h for block in CONFIG["hooks"][event] for h in block["hooks"]]


def _argv(entry):
    """The argv Claude Code spawns for an exec-form hook, with the plugin root filled in."""
    return [s.replace("${CLAUDE_PLUGIN_ROOT}", str(KIT)) for s in [entry["command"]] + entry["args"]]


# ---- registration ------------------------------------------------------------

def test_the_three_events_are_registered():
    assert sorted(CONFIG["hooks"]) == ["PreToolUse", "SessionStart", "UserPromptSubmit"]
    (gate,) = _entries("PreToolUse")
    (arm,) = _entries("UserPromptSubmit")
    (check,) = _entries("SessionStart")
    assert CONFIG["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert _argv(gate)[1:] == ["2", str(HOOKS / "merge_gate.py")]
    assert _argv(arm)[1:] == ["0", str(HOOKS / "phrase_arm.py")]
    assert _argv(check)[1:] == ["--check"]


@pytest.mark.parametrize("event", ["PreToolUse", "UserPromptSubmit", "SessionStart"])
def test_every_hook_is_exec_form_through_run_py(event):
    for entry in _entries(event):
        assert entry["type"] == "command"
        argv = _argv(entry)
        assert argv[0] == str(RUN_PY)
        assert all(Path(a).exists() for a in argv if a.startswith(str(KIT)))


def test_run_py_is_executable():
    assert os.access(RUN_PY, os.X_OK)


def test_the_gate_fails_closed_when_it_cannot_run():
    """onFailure: block turns a hook that cannot start, crashes or times out into a
    block (Claude Code v2.1.295+). Without it each of those lets the command through."""
    (gate,) = _entries("PreToolUse")
    assert gate.get("onFailure") == "block"


def test_a_failing_phrase_arm_never_blocks_the_prompt():
    """On UserPromptSubmit a blocking failure would swallow the human's prompt."""
    (arm,) = _entries("UserPromptSubmit")
    assert arm.get("onFailure", "continue") == "continue"


def test_the_guard_and_the_notifier_finish_before_the_hook_timeout():
    """At its timeout Claude Code cancels the hook and discards its output, so the
    agent never learns the merge was armed."""
    (arm,) = _entries("UserPromptSubmit")
    assert phrase_arm.GUARD_TIMEOUT_S + merge_marker.NOTIFY_TIMEOUT_S < arm["timeout"]


# ---- run-py ------------------------------------------------------------------

def _bin(tmp_path, **pythons):
    """A PATH directory: name -> "real" (a link to this interpreter) or "old"
    (a python that fails the version check)."""
    d = tmp_path / "bin"
    d.mkdir()
    for name, kind in pythons.items():
        p = d / name.replace("_", ".")
        if kind == "real":
            p.symlink_to(sys.executable)
        else:
            p.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            p.chmod(0o755)
    return d


def _run_py(bin_dir, *args, stdin=b""):
    env = {"PATH": str(bin_dir), "HOME": os.environ.get("HOME", "/")}
    return subprocess.run([str(RUN_PY), *args], input=stdin, env=env,
                          capture_output=True, timeout=60)


@pytest.fixture
def probe(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text("import sys; print(sys.argv[1:]); sys.exit(7)\n", encoding="utf-8")
    return script


def test_run_py_skips_a_python_below_3_10(tmp_path, probe):
    bin_dir = _bin(tmp_path, python3="old", python3_12="real")
    r = _run_py(bin_dir, "2", str(probe), "a", "b c")
    assert (r.returncode, r.stdout, r.stderr) == (7, b"['a', 'b c']\n", b""), r


def test_run_py_prefers_python3_when_it_is_new_enough(tmp_path, probe):
    bin_dir = _bin(tmp_path, python3="real", python3_12="old")
    assert _run_py(bin_dir, "2", str(probe)).returncode == 7


@pytest.mark.parametrize("rc", ["2", "0"])
def test_run_py_without_a_python_exits_with_the_code_given(tmp_path, probe, rc):
    bin_dir = _bin(tmp_path, python3="old")
    r = _run_py(bin_dir, rc, str(probe))
    assert r.returncode == int(rc), r
    assert r.stdout == b""
    assert b"no Python 3.10 or newer" in r.stderr


def test_check_is_silent_when_a_python_is_found(tmp_path):
    r = _run_py(_bin(tmp_path, python3_11="real"), "--check")
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b""), r


def test_check_tells_the_human_how_to_fix_a_missing_python(tmp_path):
    r = _run_py(_bin(tmp_path, python3="old"), "--check")
    assert r.returncode == 0, r
    out = json.loads(r.stdout)
    assert "no Python 3.10 or newer" in out["systemMessage"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert out["hookSpecificOutput"]["additionalContext"] == out["systemMessage"]


# ---- the gate as hooks.json launches it ----------------------------------------

def _fake_gh(tmp_path, base):
    d = tmp_path / "gh-bin"
    d.mkdir()
    gh = d / "gh"
    gh.write_text("#!/bin/sh\necho %s\n" % shlex.quote(base), encoding="utf-8")
    gh.chmod(0o755)
    return d


def _gate(env_extra, command, tmp_path, base="main"):
    """The gate as hooks.json launches it, with a fake gh first on PATH. The caller's
    gate variables and PYTHONPATH are scrubbed, as test_merge_gate.py's harness does,
    so nothing from the test runner's environment loads into the gate."""
    (entry,) = _entries("PreToolUse")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_OPTION_NOTIFY_COMMAND", "PYTHONPATH")}
    env["PATH"] = str(_fake_gh(tmp_path, base)) + os.pathsep + env.get("PATH", "")
    env.update(env_extra)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                          "session_id": "s1"}).encode("utf-8")
    return subprocess.run(_argv(entry), input=payload, env=env, capture_output=True,
                          timeout=60)


def test_the_registered_gate_blocks_a_push_to_main(tmp_path):
    r = _gate({}, "git push origin main", tmp_path)
    assert r.returncode == 2, r
    assert b"! git push origin main" in r.stderr


def test_the_registered_gate_allows_an_ordinary_command(tmp_path):
    r = _gate({}, "ls -la", tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b""), r


# ---- G2: the marker the phrase arm writes is the one the gate consumes ----------

def test_a_phrase_arm_marker_is_consumed_by_the_gate(tmp_path, monkeypatch):
    data = tmp_path / "plugin-data"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_NOTIFY_COMMAND", raising=False)
    monkeypatch.setattr(phrase_arm, "run_guard", lambda pr, cwd=None, repo=None: (0, "GO"))

    context = phrase_arm.run({"prompt": "mgo 917", "session_id": "s1"})
    assert "ALREADY armed" in context
    marker = data / "cc-merge-auth.917.json"
    assert marker.is_file()

    r = _gate({"CLAUDE_PLUGIN_DATA": str(data)}, "gh pr merge 917 --squash", tmp_path)
    assert (r.returncode, r.stderr) == (0, b""), r
    assert not marker.exists(), "the gate consumed the marker the phrase arm wrote"


def test_without_plugin_data_neither_side_arms_or_clears(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(phrase_arm, "run_guard", lambda pr, cwd=None, repo=None: (0, "GO"))

    context = phrase_arm.run({"prompt": "mgo 917", "session_id": "s1"})
    assert "NOT armed" in context
    assert not list(tmp_path.glob("**/cc-merge-auth.*"))

    r = _gate({}, "gh pr merge 917 --squash", tmp_path)
    assert r.returncode == 2, r
    assert b"reject=nodir" in r.stderr
