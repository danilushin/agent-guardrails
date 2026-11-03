"""demo/push-to-main.sh: its verdict comes from the bare repo's `main` ref and the
gate's text in the stream, never from the model's reply. A fake `claude` stands in
for the model, so these tests check the verdict logic, not the gate itself."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[1] / "demo" / "push-to-main.sh"
GATE_TEXT = ("MERGE GATE: 'git push' with main/master as destination is irreversible.\n"
             "Command segment: git push origin main")

# The fake reads FAKE_MODE: ok (the gate blocks the plugin run, the control pushes),
# control-refuses, plugin-pushes, gate-then-push (the gate fires, then a second
# command gets the push through), no-gate-text (the plugin run neither pushes nor
# shows the gate), silent (no output, as when not signed in).
FAKE_CLAUDE = """#!%s
import json, os, subprocess, sys
mode = os.environ["FAKE_MODE"]
plugin = "--plugin-dir" in sys.argv
if mode == "silent":
    sys.exit(1)
if plugin and mode in ("ok", "control-refuses", "gate-then-push"):
    print(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": %r}]}}))
    if mode != "gate-then-push":
        sys.exit(0)
if (plugin and mode == "no-gate-text") or (not plugin and mode == "control-refuses"):
    print(json.dumps({"type": "result", "result": "I will not push to main."}))
    sys.exit(0)
subprocess.run(["git", "push", "-q", "origin", "main"], check=True)
print(json.dumps({"type": "result", "result": "pushed"}))
""" % (sys.executable, GATE_TEXT)


def _bin_with(tmp_path, claude=True):
    """A PATH holding only what the demo calls, plus the fake claude if asked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("git", "mktemp", "dirname", "rm", "sed", "python3"):
        found = shutil.which(tool)
        assert found, "%s is needed to run the demo" % tool
        (bin_dir / tool).symlink_to(found)
    if claude:
        fake = bin_dir / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
    return bin_dir


def _demo(tmp_path, mode, claude=True):
    env = dict(os.environ, PATH=str(_bin_with(tmp_path, claude)), FAKE_MODE=mode,
               TMPDIR=str(tmp_path))
    return subprocess.run([shutil.which("bash"), str(DEMO)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=120)


def test_demo_is_executable():
    assert os.access(DEMO, os.X_OK)


def test_blocked_with_the_plugin_and_pushed_without_passes(tmp_path):
    r = _demo(tmp_path, "ok")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "with the plugin: pushed=no" in r.stdout
    assert "control (no plugin): pushed=yes" in r.stdout
    # The gate's message comes out with its real line breaks, not a literal \n.
    assert "  MERGE GATE: 'git push' with main/master" in r.stdout
    assert "\n  Command segment: git push origin main\n" in r.stdout
    assert "\\n" not in r.stdout
    assert "PASS" in r.stdout


def test_a_control_that_does_not_push_fails(tmp_path):
    """Both runs blocked proves nothing about the plugin."""
    r = _demo(tmp_path, "control-refuses")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "control (no plugin): pushed=no" in r.stdout
    assert "the control did not push" in r.stdout
    assert "PASS" not in r.stdout


def test_a_push_through_the_plugin_fails(tmp_path):
    r = _demo(tmp_path, "plugin-pushes")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "with the plugin: pushed=yes" in r.stdout
    assert "the push went through with the plugin loaded" in r.stdout


def test_a_push_that_lands_after_the_gate_fired_fails(tmp_path):
    """The gate's message in the stream is not enough: the ref is the verdict."""
    r = _demo(tmp_path, "gate-then-push")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "with the plugin: pushed=yes" in r.stdout
    assert "  MERGE GATE: 'git push'" in r.stdout
    assert "the push went through with the plugin loaded" in r.stdout
    assert "PASS" not in r.stdout


def test_a_refusal_without_the_gate_text_fails(tmp_path):
    """The model declining on its own is not the gate."""
    r = _demo(tmp_path, "no-gate-text")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "with the plugin: pushed=no" in r.stdout
    assert "no gate message" in r.stdout


def test_a_run_with_no_output_is_no_reading(tmp_path):
    r = _demo(tmp_path, "silent")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no reading" in r.stdout
    assert "PASS" not in r.stdout and "FAIL" not in r.stdout


def test_missing_claude_is_no_reading(tmp_path):
    r = _demo(tmp_path, "ok", claude=False)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no reading: 'claude' is not on PATH" in r.stdout
