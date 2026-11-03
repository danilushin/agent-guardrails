"""hooks/merge_marker.py: the one-shot merge authorization, its directory, its arm log
and the optional notify_command.

The marker lives in $CLAUDE_PLUGIN_DATA, which Claude Code exports to every hook
process of the plugin. Both the phrase arm (writer) and the merge gate (reader) get
their path from marker_dir() here, so they cannot disagree; with the variable unset
there is no directory, and arming refuses rather than writing somewhere the gate
never reads.
"""
import json
import os
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))
import merge_marker  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "plugin-data"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(d))
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_NOTIFY_COMMAND", raising=False)
    return d


def _iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _write_marker(d, target, ts, session_id="other-session"):
    d.mkdir(parents=True, exist_ok=True)
    path = d / merge_marker.marker_name(target)
    path.write_text(json.dumps({"target": target, "ts": ts, "note": "sibling arm",
                                "session_id": session_id}), encoding="utf-8")
    return path


# ---- the directory -----------------------------------------------------------

def test_marker_dir_is_claude_plugin_data(data_dir):
    assert merge_marker.marker_dir() == str(data_dir)
    assert merge_marker.marker_path("1778") == str(data_dir / "cc-merge-auth.1778.json")


@pytest.mark.parametrize("value", [None, ""])
def test_no_plugin_data_means_no_marker_dir(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", value)
    assert merge_marker.marker_dir() is None
    with pytest.raises(merge_marker.NoMarkerDir):
        merge_marker.marker_path("1778")


def test_arm_without_plugin_data_refuses_and_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(merge_marker.NoMarkerDir):
        merge_marker.arm("1778", "mgo 1778")
    assert list(tmp_path.iterdir()) == []


# ---- names -------------------------------------------------------------------

def test_marker_name_is_per_target():
    assert merge_marker.marker_name("1778") == "cc-merge-auth.1778.json"
    assert merge_marker.marker_name(42) == "cc-merge-auth.42.json"


@pytest.mark.parametrize("bad", ["../x", "1778/..", "", "12345678", "push-main", "17 78",
                                 "1778.json", "-1"])
def test_marker_name_refuses_anything_but_a_pr_number(bad):
    """The target becomes part of a path. push-main is gone too: no marker ever clears
    a push to main, so there is nothing for such a file to do."""
    with pytest.raises(ValueError):
        merge_marker.marker_name(bad)


# ---- arm ---------------------------------------------------------------------

def test_arm_writes_the_marker(data_dir):
    payload = merge_marker.arm("1778", "  mgo 1778  ", session_id="sess-9")
    data = json.loads((data_dir / "cc-merge-auth.1778.json").read_text(encoding="utf-8"))
    assert data == payload
    assert data["target"] == "1778"
    assert data["note"] == "mgo 1778"
    assert data["session_id"] == "sess-9"
    assert datetime.fromisoformat(data["ts"]).tzinfo is not None


def test_arm_creates_the_data_dir(data_dir):
    assert not data_dir.exists()
    merge_marker.arm("1778", "mgo 1778")
    assert (data_dir / "cc-merge-auth.1778.json").is_file()


def test_arm_takes_no_session_id_from_the_environment(data_dir, monkeypatch):
    """The phrase hook passes the payload's session id. Nothing else is trusted."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sess")
    assert merge_marker.arm("1778", "mgo 1778")["session_id"] == ""


def test_a_sibling_arm_for_another_target_is_untouched(data_dir):
    sibling = _write_marker(data_dir, "2335", _iso(0.5), session_id="sibling-sid")
    before = sibling.read_bytes()
    merge_marker.arm("2337", "mgo 2337", session_id="mine")
    assert sibling.read_bytes() == before
    mine = json.loads((data_dir / "cc-merge-auth.2337.json").read_text(encoding="utf-8"))
    assert mine["session_id"] == "mine"


@pytest.mark.parametrize("old", [
    pytest.param(lambda d: _write_marker(d, "2337", _iso(merge_marker.MARKER_TTL_MINUTES + 1)),
                 id="expired"),
    pytest.param(lambda d: _write_marker(d, "2337", _iso(0.5), session_id="pre-clear-sid"),
                 id="live-same-target"),
    pytest.param(lambda d: (d.mkdir(parents=True, exist_ok=True),
                            (d / "cc-merge-auth.2337.json").write_text("{not json")),
                 id="unparseable"),
])
def test_arm_overwrites_the_same_target(data_dir, old):
    old(data_dir)
    merge_marker.arm("2337", "mgo 2337", session_id="post-clear-sid")
    data = json.loads((data_dir / "cc-merge-auth.2337.json").read_text(encoding="utf-8"))
    assert (data["note"], data["session_id"]) == ("mgo 2337", "post-clear-sid")


def test_arm_leaves_no_temp_file_and_no_single_slot_file(data_dir):
    merge_marker.arm("2337", "mgo 2337")
    assert sorted(p.name for p in data_dir.iterdir()) == ["arm-log.jsonl", "cc-merge-auth.2337.json"]


def test_ttl_is_ten_minutes():
    assert merge_marker.MARKER_TTL_MINUTES == 10


# ---- the arm log -------------------------------------------------------------

def test_every_arm_appends_one_line_to_the_arm_log(data_dir):
    first = merge_marker.arm("1", "mgo 1", session_id="a")
    second = merge_marker.arm("2", "mgo 2", session_id="b")
    lines = (data_dir / "arm-log.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln) for ln in lines] == [first, second]


# ---- notify_command ----------------------------------------------------------

def _notifier(tmp_path, body):
    script = tmp_path / "notify.py"
    script.write_text(body, encoding="utf-8")
    return "%s %s" % (shlex.quote(sys.executable), shlex.quote(str(script)))


def test_notify_command_gets_the_arm_record_on_stdin(data_dir, tmp_path, monkeypatch):
    out = tmp_path / "notified.json"
    cmd = _notifier(tmp_path, "import sys\nopen(sys.argv[1], 'w').write(sys.stdin.read())\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_NOTIFY_COMMAND", cmd + " " + shlex.quote(str(out)))
    payload = merge_marker.arm("1778", "mgo 1778", session_id="s1")
    assert json.loads(out.read_text(encoding="utf-8")) == payload


def test_notify_command_runs_without_a_shell(data_dir, tmp_path, monkeypatch):
    """Exec form: shell syntax in the option is passed as plain arguments. A shell
    would expand the double-quoted "$HOME" and run `b` after the unquoted `;`."""
    out = tmp_path / "argv.json"
    cmd = _notifier(tmp_path, "import json, sys\njson.dump(sys.argv[2:], open(sys.argv[1], 'w'))\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_NOTIFY_COMMAND",
                       cmd + " " + shlex.quote(str(out)) + ' "$HOME" a;b')
    merge_marker.arm("1778", "mgo 1778")
    assert json.loads(out.read_text(encoding="utf-8")) == ["$HOME", "a;b"]


@pytest.mark.parametrize("command", [
    pytest.param("/no/such/notifier", id="missing-executable"),
    pytest.param("%s -c 'import sys; sys.exit(3)'" % shlex.quote(sys.executable), id="non-zero"),
    pytest.param("'unbalanced", id="unparseable"),
])
def test_a_failing_notify_command_never_stops_the_arm(data_dir, monkeypatch, capsys, command):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_NOTIFY_COMMAND", command)
    merge_marker.arm("1778", "mgo 1778")
    assert (data_dir / "cc-merge-auth.1778.json").is_file()
    assert "notify_command" in capsys.readouterr().err


def test_a_hung_notify_command_is_cut_off(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(merge_marker, "NOTIFY_TIMEOUT_S", 0.5)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_NOTIFY_COMMAND",
                       "%s -c 'import time; time.sleep(30)'" % shlex.quote(sys.executable))
    merge_marker.arm("1778", "mgo 1778")
    assert (data_dir / "cc-merge-auth.1778.json").is_file()
    assert "notify_command" in capsys.readouterr().err


def test_a_blank_notify_command_runs_nothing(data_dir, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_NOTIFY_COMMAND", "   ")
    merge_marker.arm("1778", "mgo 1778")
    assert capsys.readouterr().err == ""
