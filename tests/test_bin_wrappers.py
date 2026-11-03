"""bin/ wrappers: Claude Code puts the plugin's bin/ on the Bash tool's PATH, and
CLAUDE_PLUGIN_ROOT is not in that environment, so each wrapper finds its script
from its own location. Run from an unrelated cwd, each must reach its script."""
import os
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
WRAPPERS = {
    "premerge-guard": "Pre-merge base-freshness gate",
    "ci-wait": "--deadline",
    "dup-scan": "--prebranch",
}


@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_wrapper_is_executable(name):
    assert os.access(BIN / name, os.X_OK), "%s must carry the executable bit" % name


@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_wrapper_runs_its_script_from_any_cwd(name, tmp_path):
    r = subprocess.run([str(BIN / name), "--help"], cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert WRAPPERS[name] in r.stdout


def test_wrapper_passes_arguments_through(tmp_path):
    """An argparse error proves the arguments reached the script, not the shell."""
    r = subprocess.run([str(BIN / "ci-wait"), "--sha", "abc", "--no-such-flag"],
                       cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "--no-such-flag" in r.stderr
