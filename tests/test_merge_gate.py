"""Real-gate tests for hooks/merge_gate.py.

Every case runs the actual merge_gate.py as a subprocess, the way Claude Code does: a
JSON PreToolUse payload on stdin, the verdict in the exit code (0 allow, 2 block). The
`gh` it probes is a PATH-stubbed fake that answers `baseRefName` from FAKE_GH_BASES and
logs every call, so the probe is offline and its use is observable. The marker
directory is CLAUDE_PLUGIN_DATA, as Claude Code sets it for plugin hooks.

Coverage:
  - the detection corpus, each GATED merge case under a main base, a dev base and an
    armed marker;
  - the #3028 shapes: a push chained after an allowed merge, and quoted / @ destinations;
  - the marker protocol: one-shot consume, 10-min TTL, session binding, wrong target,
    and no CLAUDE_PLUGIN_DATA (reject=nodir);
  - a push to main: blocks in every directory, and the message carries one `!` line
    (the command up to the push, in the payload cwd);
  - the review shapes: quote-aware comments, the detection-view push check,
    continuations, every merge/REST segment, quoted/attached PUT, quoted PR numbers,
    the probe's repo, push --all/--mirror/globs;
  - fail-closed plumbing: bad stdin, startup errors, gh not found, a hung gh probe and
    the per-command probe budget.

The corpus and the review shapes run on a thread pool (module fixtures), so the ~150
gate subprocesses stay a few seconds.

Silence twin: every allowed case asserts rc 0 AND empty stdout AND empty stderr, so a
crash can never pass as quiet. Every block asserts rc 2 exactly.
"""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1]
GATE = KIT / "hooks" / "merge_gate.py"
sys.path.insert(0, str(KIT / "hooks"))
import merge_marker  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate_mod = _load("_merge_gate_under_test", GATE)
ALLOW, BLOCK = 0, 2

# The fake `gh`: `pr view <N> ... [--repo R]` prints FAKE_GH_BASES["R#N"] when that key
# exists, else FAKE_GH_BASES[N], and exits 0; an unknown PR exits 1 the way the real gh
# does ("Could not resolve to a PullRequest"). The "R#N" key lets a case give the cwd's
# PR N and repo R's PR N different bases, so a probe that asks the wrong repo shows up
# as a different verdict. Every call's argv is appended to FAKE_GH_LOG, so a test can
# prove the probe ran (or did not).
FAKE_GH_SRC = '''\
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_GH_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\\n")
bases = json.loads(os.environ.get("FAKE_GH_BASES") or "{}")
if len(args) >= 3 and args[0] == "pr" and args[1] == "view":
    keys = [args[2]]
    if "--repo" in args[3:-1]:
        keys.insert(0, args[args.index("--repo") + 1] + "#" + args[2])
    for key in keys:
        if key in bases:
            sys.stdout.write(bases[key] + "\\n")
            sys.exit(0)
sys.stderr.write("GraphQL: Could not resolve to a PullRequest (fake gh)\\n")
sys.exit(1)
'''

# Variables the gate (or the fake gh) reads, scrubbed so the caller's env cannot leak in.
_SCRUB = ("CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_OPTION_NOTIFY_COMMAND", "FAKE_GH_BASES",
          "FAKE_GH_LOG", "PYTHONPATH")


def make_fake_gh(bin_dir):
    """Write a fake `gh` into bin_dir: a POSIX sh shim."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    impl = bin_dir / "fake_gh_impl.py"
    impl.write_text(FAKE_GH_SRC, encoding="utf-8")
    shim = bin_dir / "gh"
    shim.write_text("#!/bin/sh\nexec %s %s \"$@\"\n"
                    % (shlex.quote(sys.executable), shlex.quote(str(impl))),
                    encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir


def plant_marker(marker_dir, target, session_id="", age_minutes=0.0):
    """Write a marker directly, the way the phrase arm would."""
    path = Path(marker_dir) / merge_marker.marker_name(target)
    path.write_text(json.dumps({
        "target": target,
        "ts": (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat(),
        "note": "mgo %s" % target,
        "session_id": session_id,
    }), encoding="utf-8")
    return path


def gate_env(bin_dir, marker_dir, bases, log):
    """The gate's environment: fake gh first on PATH, the caller's gate vars scrubbed."""
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["FAKE_GH_BASES"] = json.dumps(bases)
    env["FAKE_GH_LOG"] = str(log)
    if marker_dir is not None:
        env["CLAUDE_PLUGIN_DATA"] = str(marker_dir)
    return env


def payload_bytes(command, cwd="", session_id=None, tool_name="Bash"):
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    if cwd:
        payload["cwd"] = cwd
    if session_id is not None:
        payload["session_id"] = session_id
    return json.dumps(payload).encode("utf-8")


class Run:
    def __init__(self, proc, gh_calls):
        self.rc = proc.returncode
        self.out = proc.stdout.decode("utf-8", "replace")
        self.err = proc.stderr.decode("utf-8", "replace")
        self.gh_calls = gh_calls

    def __repr__(self):
        return "Run(rc=%r, out=%r, err=%r, gh_calls=%r)" % (
            self.rc, self.out, self.err, self.gh_calls)


class GateHarness:
    """One isolated fixture home: marker dir, fake gh, call log."""

    def __init__(self, root):
        self.root = Path(root)
        self.markers = self.root / "plugin-data"
        self.markers.mkdir(parents=True, exist_ok=True)
        self.bin = make_fake_gh(self.root / "bin")
        self.log = self.root / "gh-calls.jsonl"
        self.bases = {}

    def plant(self, target, session_id="", age_minutes=0.0):
        return plant_marker(self.markers, target, session_id, age_minutes)

    def marker_files(self):
        return sorted(p.name for p in self.markers.iterdir())

    def env(self, marker_dir=True):
        return gate_env(self.bin, self.markers if marker_dir else None, self.bases, self.log)

    def gh_calls(self):
        if not self.log.exists():
            return []
        calls = [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines() if ln]
        self.log.unlink()
        return calls

    def run_raw(self, raw, marker_dir=True, extra_env=None):
        env = self.env(marker_dir=marker_dir)
        env.update(extra_env or {})
        proc = subprocess.run([sys.executable, str(GATE)], input=raw, env=env,
                              capture_output=True, timeout=60)
        return Run(proc, self.gh_calls())

    def run(self, command, cwd="", session_id=None, **kw):
        return self.run_raw(payload_bytes(command, cwd, session_id), **kw)


@pytest.fixture
def gate(tmp_path):
    return GateHarness(tmp_path)


def assert_allowed(r):
    """Silence twin: rc 0 AND nothing on stdout AND nothing on stderr."""
    assert (r.rc, r.out, r.err) == (ALLOW, "", ""), r


def assert_blocked(r):
    assert r.rc == BLOCK, r
    assert r.out == "", r
    assert "MERGE GATE" in r.err, r


def bang_lines(err):
    return [ln.strip() for ln in err.splitlines() if ln.strip().startswith("! ")]


# ---------------------------------------------------------------------------
# The gate and the writer share one marker protocol
# ---------------------------------------------------------------------------

def test_ttl_comes_from_the_writer():
    assert gate_mod.TTL_MINUTES == merge_marker.MARKER_TTL_MINUTES == 10


def test_marker_file_name_and_dir_match_the_writer(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    for target in ("917", "1", "9999999"):
        assert gate_mod.marker_path(target) == merge_marker.marker_path(target)
        assert Path(gate_mod.marker_path(target)).parent == tmp_path


# ---------------------------------------------------------------------------
# The detection corpus, replayed against the REAL gate
# ---------------------------------------------------------------------------

ALLOW_V = "ALLOW"
BLOCK_V = "BLOCK"   # blocked before any probe/marker (no resolvable target)


def gated(target):
    return "GATED:" + target


PUSH_MAIN = gated("push")
APP = "/work/app"
HOME_REPO = "/work/home-repo"

CASES = [
    # -- no-op / pass-through
    ("ls -la", APP, ALLOW_V, "01-non-git"),
    ("git push origin dev", APP, ALLOW_V, "02-push-dev"),
    ("git push origin feat/main-dashboard", APP, ALLOW_V, "03-branch-containing-main"),
    ("git push origin dev  # sync with main later", APP, ALLOW_V, "04-comment-mentions-main"),
    ("git checkout main && git push origin dev", APP, ALLOW_V, "05-checkout-main-push-dev"),
    # -- push-to-main
    ("git push origin main", APP, PUSH_MAIN, "09-push-origin-main"),
    ("git push origin HEAD:main", APP, PUSH_MAIN, "10-push-HEAD-main"),
    ("# promoting dev to main per authorization\ngit push origin dev:main", APP, PUSH_MAIN,
     "11-push-under-context-comment"),
    ("git push origin master", APP, PUSH_MAIN, "push-master"),
    ("git push origin +main", APP, PUSH_MAIN, "push-force-plus-main"),
    # -- #3028 (b): quoted / @ destinations
    ('git push origin "main"', APP, PUSH_MAIN, "3028b-double-quoted"),
    ("git push origin 'main'", APP, PUSH_MAIN, "3028b-single-quoted"),
    ('git push origin HEAD:"main"', APP, PUSH_MAIN, "3028b-HEAD-colon-quoted"),
    ("git push origin @:main", APP, PUSH_MAIN, "3028b-at-colon"),
    ("git push origin HEAD~1:main", APP, PUSH_MAIN, "3028b-tilde-src"),
    ('git push origin "main', APP, PUSH_MAIN, "3028b-unbalanced-quote-fails-closed"),
    ('git push origin "dev"', APP, ALLOW_V, "3028b-quoted-dev"),
    ('git push origin "main:dev"', APP, ALLOW_V, "3028b-quoted-main-as-source"),
    ("git push origin 'feat/main-dashboard'", APP, ALLOW_V, "3028b-quoted-branch-containing-main"),
    # -- #3167: the continuation fold deletes the pair, so a continuation inside a word joins it
    ("git pu\\\nsh origin main", APP, PUSH_MAIN, "3167-mid-word-push"),
    ("git push origin ma\\\nin", APP, PUSH_MAIN, "3167-mid-word-destination"),
    # -- #3028 (i): pushes that can update main without naming it
    ("git push --all origin", APP, PUSH_MAIN, "3028i-push-all"),
    ("git push origin 'refs/heads/*:refs/heads/*'", APP, PUSH_MAIN, "3028i-glob-refspec"),
    ("git push -o 'ci.variable=X=*' origin dev", APP, ALLOW_V, "3028i-star-in-an-option-is-not-a-glob"),
    ("gh pr mer\\\nge 1398 --squash", HOME_REPO, gated("1398"), "3167-mid-word-merge"),
    # -- gh pr merge
    ("gh pr merge --squash", APP, BLOCK_V, "12-merge-without-number"),
    ("gh pr merge --squash --delete-branch", APP, BLOCK_V, "merge-without-number-2"),
    ("gh pr merge 1398 --squash", HOME_REPO, gated("1398"), "13-merge-by-number"),
    ("gh pr merge https://github.com/octo/home-repo/pull/1398 --squash", HOME_REPO,
     gated("1398"), "15-merge-by-url"),
    ("GH PR MERGE 7 --squash", HOME_REPO, gated("7"), "merge-uppercase-is-case-insensitive"),
    # -- #3028 (g): a number or URL inside a quoted flag value never stands in for the PR
    ('gh pr merge --squash --subject "fix 10 things" 11', HOME_REPO, gated("11"), "3028g-quoted-subject-number"),
    ("gh pr merge --squash --body 'see 10 later' 11", HOME_REPO, gated("11"), "3028g-single-quoted-body-number"),
    ('gh pr merge --squash --subject "see https://github.com/o/r/pull/10" 11', HOME_REPO, gated("11"),
     "3028g-quoted-url-in-subject"),
    ('gh pr merge "https://github.com/o/r/pull/11" --squash', HOME_REPO, gated("11"), "3028g-quoted-url-still-read"),
    ('gh pr merge "11" --squash', HOME_REPO, BLOCK_V, "3028g-quoted-bare-number-is-not-read"),
    # -- #1421 / #1471 inert mentions
    ('git commit -m "the probe failed on every gh pr merge"', APP, ALLOW_V, "22-quoted-commit-msg"),
    ("cat > file.md << 'EOF'\ncontains the phrase gh pr merge for docs\nEOF", APP, ALLOW_V,
     "23-heredoc-to-file"),
    # #2815: the segment walk picks the first segment whose own detection view matches, so
    # quoted noise never shadows the real merge.
    ('git commit -m "unrelated gh pr merge note" && gh pr merge 1398 --squash', HOME_REPO, gated("1398"),
     "24-real-merge-after-quoted-noise"),
    # #2815: a heredoc fed to `git commit -F -` is a commit message -- data, like cat/tee.
    ("git add x && git commit -q -F - <<'EOF'\nfix: mentions gh pr merge and git push origin main\nEOF",
     APP, ALLOW_V, "2815-commit-F-dash-heredoc"),
    ('git commit --file=- <<"EOF"\nsee gh pr merge 5\nEOF', APP, ALLOW_V, "2815-commit-file-equals-dash"),
    # ...but the sink must not swallow a real merge sharing its line or following it.
    ("git commit -m x && gh pr merge 5 && git commit -F - <<'EOF'\nbody\nEOF", HOME_REPO, gated("5"),
     "2815-real-merge-before-the-sink-on-one-line"),
    ("git commit -F - <<'EOF'\nbody\nEOF\ngh pr merge 7 --squash", HOME_REPO, gated("7"),
     "2815-real-merge-after-the-heredoc"),
    # -- #3161: a gh create/comment/edit body read from stdin (`-F -` / `--body-file -`)
    # is data as well: a PR body quoting a merge must not block the create.
    ("gh pr create --base main --title t --body-file - <<'EOF'\n"
     "test: -m \"fix #123\" && gh pr merge 11 --squash\nEOF", APP, ALLOW_V, "3161-pr-create-body-file-heredoc"),
    ("gh issue create --title t -F - <<'EOF'\nsee gh pr merge 11 and git push origin main\nEOF", APP, ALLOW_V,
     "3161-issue-create-short-F"),
    ('gh pr comment 5 --body-file=- <<"EOF"\nrun gh pr merge 5\nEOF', APP, ALLOW_V, "3161-pr-comment-body-file-equals"),
    # ...but never a real merge after the heredoc, before it, or chained on its operator
    # line: unlike the cat/tee/commit sinks, this one allows only blanks after the marker.
    ("gh pr create --body-file - <<'EOF'\nbody\nEOF\ngh pr merge 7 --squash", HOME_REPO, gated("7"),
     "3161-real-merge-after-the-heredoc"),
    ("gh pr merge 7 --squash && gh pr create --body-file - <<'EOF'\nbody\nEOF", HOME_REPO, gated("7"),
     "3161-real-merge-before-the-sink"),
    ("gh pr create --body-file - <<'EOF' && gh pr merge 7 --squash\nbody\nEOF", HOME_REPO, gated("7"),
     "3161-merge-chained-on-the-operator-line"),
    ("python - <<'EOF'\ngh pr merge 9\nEOF", HOME_REPO, gated("9"), "2815-interpreter-heredoc-still-code"),
    ("bash << 'EOF'\ngh pr merge 1398 --squash\nEOF", HOME_REPO, gated("1398"), "25-heredoc-to-interpreter"),
    ('gh issue comment 1397 --repo x/y --body "It blocks a gh pr merge targeting main.\nSee candidate #1."',
     "", ALLOW_V, "1471-multiline-hash-before-quote"),
    ('gh pr comment 1397 --repo x/y --body "the gate blocks a gh pr merge to main. see #12."', "", ALLOW_V,
     "1471-pr-comment"),
    ("gh issue comment 1397 --repo x/y --body 'mentions gh pr merge in prose'", "", ALLOW_V,
     "1421-single-quoted"),
    ("git status # reminder to git push origin main later", "", ALLOW_V, "1421-trailing-comment"),
    # -- #2803 REST merge endpoint
    ("gh api -X PUT repos/nobody-xyz/never-exists-2803/pulls/5/merge -f merge_method=squash", HOME_REPO,
     gated("5"), "2803-dash-X-PUT"),
    ("gh api --method PUT repos/nobody-xyz/never-exists-2803/pulls/5/merge", HOME_REPO, gated("5"),
     "2803-long-method"),
    ("gh api --method=PUT repos/nobody-xyz/never-exists-2803/pulls/5/merge", HOME_REPO, gated("5"),
     "2803-long-method-equals"),
    ("gh api -X PUT https://api.github.com/repos/nobody-xyz/never-exists-2803/pulls/5/merge", HOME_REPO,
     gated("5"), "2803-absolute-url"),
    ("gh api -X PUT pulls/5/merge -f merge_method=squash", HOME_REPO, BLOCK_V, "2803-no-owner-repo"),
    ("gh api repos/octo/home-repo/pulls/2799/merge", HOME_REPO, ALLOW_V, "2803-GET-is-a-read"),
    ("gh api -X GET repos/octo/home-repo/pulls/2799/merge", HOME_REPO, ALLOW_V, "2803-explicit-GET"),
    ('gh issue comment 2803 --repo x/y --body "gates gh api -X PUT repos/o/r/pulls/5/merge. see #12."',
     HOME_REPO, ALLOW_V, "2803-prose-names-endpoint"),
    ("gh api repos/octo/home-repo/pulls/2799", HOME_REPO, ALLOW_V, "2803-unrelated-pr-read"),
    # -- #3028 (f): a quoted or attached method. The detection view blanks the quotes
    # and API_PUT_RE needs a separator, so API_PUT_SCAN_RE reads the scan view.
    ('gh api -X "PUT" repos/o/r/pulls/5/merge', HOME_REPO, gated("5"), "3028f-double-quoted"),
    ("gh api --method 'PUT' repos/o/r/pulls/5/merge", HOME_REPO, gated("5"), "3028f-long-single-quoted"),
    ('gh api -X"PUT" repos/o/r/pulls/5/merge', HOME_REPO, gated("5"), "3028f-attached-quoted"),
    ("gh api -XPUT repos/o/r/pulls/5/merge", HOME_REPO, gated("5"), "3028f-attached"),
    ('gh api -X "GET" repos/o/r/pulls/5/merge', HOME_REPO, ALLOW_V, "3028f-quoted-GET-is-a-read"),
]


def _ids(cases):
    return [c[3] for c in cases]


ALLOW_CASES = [c for c in CASES if c[2] == ALLOW_V]
BLOCK_CASES = [c for c in CASES if c[2] == BLOCK_V]
GATED_MERGE = [c for c in CASES if c[2].startswith("GATED:") and c[2] != PUSH_MAIN]
GATED_PUSH = [c for c in CASES if c[2] == PUSH_MAIN]


def test_the_corpus_is_whole_and_partitioned():
    """An empty list would parametrize to zero tests and read green."""
    assert len(CASES) >= 60
    assert ALLOW_CASES and BLOCK_CASES and GATED_MERGE and GATED_PUSH
    assert len(ALLOW_CASES) + len(BLOCK_CASES) + len(GATED_MERGE) + len(GATED_PUSH) == len(CASES)
    assert len({c[3] for c in CASES}) == len(CASES), "case ids must be unique"


POOL_WORKERS = min(8, os.cpu_count() or 2)


def _corpus_job(root, kind, case):
    cmd, cwd, want, cid = case
    harness = GateHarness(Path(root) / kind / cid)
    pr = want.split(":", 1)[1] if want.startswith("GATED:") else None
    if kind in ("main", "dev"):
        harness.bases = {pr: kind}
    elif kind == "armed":
        harness.bases = {pr: "main"}
        marker = harness.plant(pr)
        first = harness.run(cmd, cwd=cwd)
        consumed = not marker.exists()
        return first, consumed, harness.run(cmd, cwd=cwd)
    return (harness.run(cmd, cwd=cwd),)


CORPUS_JOBS = ([("allow", c) for c in ALLOW_CASES] + [("block", c) for c in BLOCK_CASES]
               + [(k, c) for c in GATED_MERGE for k in ("main", "dev", "armed")]
               + [("push", c) for c in GATED_PUSH])


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("corpus")
    with ThreadPoolExecutor(max_workers=POOL_WORKERS) as pool:
        out = list(pool.map(lambda job: _corpus_job(root, *job), CORPUS_JOBS))
    return {(kind, case[3]): res for (kind, case), res in zip(CORPUS_JOBS, out)}


@pytest.mark.parametrize("cmd,cwd,_want,cid", ALLOW_CASES, ids=_ids(ALLOW_CASES))
def test_corpus_allow_is_silent_and_never_probes(corpus, cmd, cwd, _want, cid):
    (r,) = corpus[("allow", cid)]
    assert_allowed(r)
    assert r.gh_calls == []


@pytest.mark.parametrize("cmd,cwd,_want,cid", BLOCK_CASES, ids=_ids(BLOCK_CASES))
def test_corpus_block_without_target_blocks_before_any_probe(corpus, cmd, cwd, _want, cid):
    (r,) = corpus[("block", cid)]
    assert_blocked(r)
    assert ("without an explicit PR number" in r.err
            or "without a resolvable owner/repo" in r.err), r
    assert r.gh_calls == []


@pytest.mark.parametrize("cmd,cwd,want,cid", GATED_MERGE, ids=_ids(GATED_MERGE))
def test_corpus_gated_merge_into_main_blocks(corpus, cmd, cwd, want, cid):
    pr = want.split(":", 1)[1]
    (r,) = corpus[("main", cid)]
    assert_blocked(r)
    assert "targets 'main'" in r.err, r
    assert len(r.gh_calls) == 1 and r.gh_calls[0][:3] == ["pr", "view", pr], r


@pytest.mark.parametrize("cmd,cwd,want,cid", GATED_MERGE, ids=_ids(GATED_MERGE))
def test_corpus_gated_merge_into_dev_allows_after_probing(corpus, cmd, cwd, want, cid):
    pr = want.split(":", 1)[1]
    (r,) = corpus[("dev", cid)]
    assert_allowed(r)
    assert len(r.gh_calls) == 1 and r.gh_calls[0][:3] == ["pr", "view", pr], r


@pytest.mark.parametrize("cmd,cwd,want,cid", GATED_MERGE, ids=_ids(GATED_MERGE))
def test_corpus_gated_merge_is_cleared_once_by_its_marker(corpus, cmd, cwd, want, cid):
    first, consumed, replay = corpus[("armed", cid)]
    assert_allowed(first)
    assert consumed, "one-shot: the allow consumes the marker"
    assert_blocked(replay)
    assert "reject=path" in replay.err


@pytest.mark.parametrize("cmd,cwd,_want,cid", GATED_PUSH, ids=_ids(GATED_PUSH))
def test_corpus_push_to_main_blocks_with_one_line_for_the_human(corpus, cmd, cwd, _want, cid):
    (r,) = corpus[("push", cid)]
    assert_blocked(r)
    # Every corpus push is the command's last line, once continuations are joined (#3167),
    # and runs in a payload cwd with no shell-special characters, so the human's line is
    # `cd <cwd> && <that line>`.
    assert cwd, "the corpus pushes carry a cwd; this expectation assumes one"
    want = "! cd %s && %s" % (cwd, cmd.replace("\\\n", "").split("\n")[-1])
    assert bang_lines(r.err) == [want], r
    assert "mgo" not in r.err, "no authorization clears a push: the block must not offer one"
    assert r.gh_calls == []


# ---------------------------------------------------------------------------
# #3028 (a): every branch is evaluated; any block wins; consume only after all pass
# ---------------------------------------------------------------------------

def test_3028a_push_chained_after_an_armed_merge_still_blocks(gate):
    gate.bases = {"12": "main"}
    marker = gate.plant("12")
    r = gate.run("gh pr merge 12 --squash && git push origin main", cwd="/nonexistent")
    assert_blocked(r)
    # Nothing ran, but the merge is gated on its own: the human's line is the push
    # alone, in the payload cwd, and never carries the merge past the marker step.
    assert bang_lines(r.err) == ["! cd /nonexistent && git push origin main"], r
    assert "carries a merge" in r.err, r
    assert marker.exists(), "a blocked command must not consume the merge's marker"


def test_3028a_push_chained_after_a_dev_merge_blocks(gate):
    gate.bases = {"12": "dev"}
    r = gate.run("gh pr merge 12 --squash && git push origin main")
    assert_blocked(r)
    assert len(r.gh_calls) == 1, "the merge branch still probed"


def test_3028a_push_chained_after_a_dev_rest_merge_blocks(gate):
    gate.bases = {"5": "dev"}
    r = gate.run("gh api -X PUT repos/o/r/pulls/5/merge && git push origin main")
    assert_blocked(r)


def test_3028a_control_dev_merge_then_dev_push_allows(gate):
    gate.bases = {"12": "dev"}
    assert_allowed(gate.run("gh pr merge 12 --squash && git push origin dev"))


def test_3028a_control_armed_merge_then_dev_push_allows_and_consumes(gate):
    gate.bases = {"12": "main"}
    marker = gate.plant("12")
    assert_allowed(gate.run("gh pr merge 12 --squash && git push origin dev"))
    assert not marker.exists(), "every branch passed, so the marker is consumed"


def test_3028a_both_blocking_branches_are_reported(gate):
    gate.bases = {"12": "main"}
    r = gate.run("gh pr merge 12 --squash && git push origin main")
    assert_blocked(r)
    assert "PR 12 targets 'main'" in r.err, r
    assert "! git push origin main" in bang_lines(r.err), r
    assert not [ln for ln in bang_lines(r.err) if "gh pr merge" in ln and "git push" in ln], \
        "a main merge never rides inside the human's push line: %r" % (r,)


# ---------------------------------------------------------------------------
# #3028 (b): shlex-tokenised destinations
# ---------------------------------------------------------------------------

QUOTED_MAIN = [
    pytest.param('git push origin "main"', id="double-quoted"),
    pytest.param("git push origin 'main'", id="single-quoted"),
    pytest.param('git push origin HEAD:"main"', id="HEAD-colon-quoted"),
    pytest.param("git push origin @:main", id="at-colon"),
    pytest.param('git push origin "master"', id="quoted-master"),
    pytest.param("git push origin 'HEAD:refs/heads/main'", id="quoted-full-ref"),
    pytest.param("git push origin HEAD~1:main", id="tilde-src"),
    pytest.param('git push origin +"main"', id="plus-quoted"),
    pytest.param('git push origin "main', id="unbalanced-quote-fails-closed"),
]


@pytest.mark.parametrize("cmd", QUOTED_MAIN)
def test_3028b_quoted_and_at_destinations_block(gate, cmd):
    r = gate.run(cmd)
    assert_blocked(r)
    assert bang_lines(r.err) == ["! " + cmd], r


@pytest.mark.parametrize("cmd", [
    pytest.param('git push origin "dev"', id="quoted-dev"),
    pytest.param("git push origin 'feat/main-dashboard'", id="quoted-branch-containing-main"),
    pytest.param("git push origin main:dev", id="main-as-source-only"),
    pytest.param('git push origin "main-backup"', id="quoted-main-prefix"),
    pytest.param("git push origin @:dev", id="at-colon-dev"),
])
def test_3028b_controls_still_allow(gate, cmd):
    assert_allowed(gate.run(cmd))


# ---------------------------------------------------------------------------
# A push to main: no marker clears it, and no directory is exempt (G5)
# ---------------------------------------------------------------------------

def test_no_marker_clears_a_push(gate):
    """Even a marker file named for a push target is never read."""
    stray = gate.markers / "cc-merge-auth.push-main.json"
    stray.write_text(json.dumps({"target": "push-main",
                                 "ts": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    r = gate.run("git push origin main", cwd="/nonexistent")
    assert_blocked(r)
    assert bang_lines(r.err) == ["! cd /nonexistent && git push origin main"], r
    assert stray.exists()


@pytest.mark.parametrize("cmd,cwd", [
    pytest.param("git push origin main", "~/.claude/notes", id="cwd-under-the-config-dir"),
    pytest.param("git -C ~/.claude/notes push origin main", "/work/app", id="dash-C-config-dir"),
    pytest.param("cd ~/.claude/notes && git push -q origin main", "/work/app", id="cd-config-dir"),
    pytest.param("git push origin main", "/", id="root"),
    pytest.param("git -C /tmp/x push origin main", "", id="no-cwd"),
])
def test_no_directory_exempts_a_push_to_main(gate, cmd, cwd):
    assert_blocked(gate.run(cmd, cwd=cwd))


def test_push_block_line_carries_the_actual_remote_and_refspec(gate):
    r = gate.run("git fetch -q && git push -u upstream HEAD:refs/heads/main")
    assert_blocked(r)
    assert bang_lines(r.err) == ["! git fetch -q && git push -u upstream HEAD:refs/heads/main"], r


def test_non_ascii_push_segment_round_trips_utf8(gate):
    """Raw UTF-8 on stdin (Hebrew + an em-dash), echoed back on stderr in the `!` line:
    the gate must neither crash nor mangle the bytes."""
    cmd = "git push -o 'ci.skip=תיקון — x' origin main"
    raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}},
                     ensure_ascii=False).encode("utf-8")
    r = gate.run_raw(raw)
    assert_blocked(r)
    assert bang_lines(r.err) == ["! " + cmd], r


# ---------------------------------------------------------------------------
# Review shapes (#3028 c-j)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Shape:
    id: str
    cmd: str
    rc: int
    bases: tuple = ()      # (key, base) answered by the fake gh; key "N" or "owner/repo#N"
    markers: tuple = ()    # targets armed before the run
    left: tuple = None     # targets whose markers must survive; None = not checked
    cwd: str = ""
    bang: tuple = None     # the exact `!` lines; None = not checked
    probes: tuple = None   # ((pr, repo or None), ...) in probe order; None = not checked
    err: str = ""          # a substring the block message must carry


def S(*args, **kw):
    return pytest.param(Shape(*args, **kw), id=args[0])


def _bang(cmd):
    return ("! " + cmd,)


C3027 = 'git commit -m "Part of #3027"'
FIX_HASH = 'git commit -m "fix #123"'
URL11 = "https://github.com/o/r/pull/11"
REPO_BASES = (("11", "dev"), ("o/r#11", "main"))   # cwd's #11 is dev, o/r#11 is main

REVIEW_SHAPES = [
    # comment: a quoted ` #` does not cut off the rest of its line.
    S("comment-part-of-then-HEAD-main", C3027 + " && git push origin HEAD:main", 2,
      bang=_bang(C3027 + " && git push origin HEAD:main")),
    S("comment-part-of-then-git-C", C3027 + " && git -C . push origin main", 2,
      bang=_bang(C3027 + " && git -C . push origin main")),
    S("comment-fix-hash-then-push-main", FIX_HASH + " && git push origin main", 2,
      bang=_bang(FIX_HASH + " && git push origin main")),
    S("comment-single-quoted-hash", "git commit -m 'see #5' && git push origin main", 2,
      bang=_bang("git commit -m 'see #5' && git push origin main")),
    S("comment-control-no-hash", 'git commit -m "fix 123" && git push origin main', 2,
      bang=_bang('git commit -m "fix 123" && git push origin main')),
    S("comment-merge-after-quoted-hash-is-cleared-by-its-marker",
      FIX_HASH + " && gh pr merge 11 --squash", 0, bases=(("11", "main"),),
      markers=("11",), left=(), probes=(("11", None),)),
    S("comment-real-comment-still-stripped", "git push origin dev  # rebased onto main", 0),
    S("comment-heredoc-apostrophe-does-not-open-a-quote",
      "git commit -F - <<'EOF'\ndon't break it\nEOF\ngit push origin dev  # rebased onto main", 0),
    S("comment-here-string-is-not-a-heredoc",
      'cat <<< "x"\ngit push origin dev  # main later', 0),
    S("comment-ending-in-backslash-does-not-continue", "# note \\\ngit push origin main", 2,
      bang=_bang("git push origin main")),
    # detview: the detection view sees a push the scan view cannot isolate.
    S("detview-quoted-semicolon-inside-git-c", "git -c 'a;b' push origin main", 2,
      bang=(), err="cannot quote the push back"),
    S("detview-control-dev", "git -c 'a;b' push origin dev", 0),
    # cont: backslash-newline continuations are folded before segmenting.
    S("cont-before-remote", "git push \\\n  origin main", 2, bang=_bang("git push   origin main")),
    S("cont-crlf-before-remote", "git push \\\r\n  origin main", 2,
      bang=_bang("git push   origin main")),
    S("cont-before-push", "git \\\n  push origin main", 2, bang=_bang("git   push origin main")),
    S("cont-before-refspec", "git push origin \\\n  HEAD:main", 2,
      bang=_bang("git push origin   HEAD:main")),
    S("cont-merge-number-on-next-line", "gh pr merge \\\n  10 --squash", 0,
      bases=(("10", "dev"),), probes=(("10", None),)),
    S("cont-control-flag-on-next-line", "git push origin main \\\n  --force", 2),
    # #3167: the fold deletes the pair, as sh does, so a continuation inside a word joins it.
    S("cont-mid-word-push", "git pu\\\nsh origin main", 2, bang=_bang("git push origin main")),
    S("cont-mid-word-destination", "git push origin ma\\\nin", 2, bang=_bang("git push origin main")),
    S("cont-mid-word-in-double-quotes", 'git push origin "ma\\\nin"', 2,
      bang=_bang('git push origin "main"')),
    S("cont-mid-word-merge", "gh pr mer\\\nge 11 --squash", 2,
      bases=(("11", "main"),), probes=(("11", None),), err="PR 11 targets 'main'"),
    # An even run of backslashes is a literal one in sh, so the push runs as its own
    # command. The gate still folds it; the block must hold.
    S("cont-escaped-backslash-still-blocks", "echo foo\\\\\ngit push origin main", 2),
    # multi: every merge / REST segment is gated, not only the first.
    S("multi-dev-merge-then-main-merge", "gh pr merge 10 --squash && gh pr merge 11 --squash", 2,
      bases=(("10", "dev"), ("11", "main")), probes=(("10", None), ("11", None)),
      err="PR 11 targets 'main'"),
    S("multi-armed-merge-then-main-merge", "gh pr merge 12 --squash && gh pr merge 11 --squash", 2,
      bases=(("12", "main"), ("11", "main")), markers=("12",), left=("12",)),
    S("multi-both-armed", "gh pr merge 12 --squash && gh pr merge 11 --squash", 0,
      bases=(("12", "main"), ("11", "main")), markers=("12", "11"), left=()),
    S("multi-rest-dev-then-rest-main",
      "gh api -X PUT repos/o/r/pulls/10/merge && gh api -X PUT repos/o/r/pulls/11/merge", 2,
      bases=(("10", "dev"), ("11", "main")), probes=(("10", "o/r"), ("11", "o/r"))),
    S("multi-rest-get-is-not-gated-beside-a-put",
      "gh api repos/o/r/pulls/11/merge && gh api -X PUT repos/o/r/pulls/10/merge", 0,
      bases=(("10", "dev"), ("11", "main")), probes=(("10", "o/r"),)),
    # put: a quoted or attached method is still a REST merge.
    S("put-double-quoted", 'gh api -X "PUT" repos/o/r/pulls/11/merge', 2,
      bases=(("11", "main"),), probes=(("11", "o/r"),)),
    S("put-long-single-quoted", "gh api --method 'PUT' repos/o/r/pulls/11/merge", 2,
      bases=(("11", "main"),), probes=(("11", "o/r"),)),
    S("put-attached-quoted", 'gh api -X"PUT" repos/o/r/pulls/11/merge', 2,
      bases=(("11", "main"),), probes=(("11", "o/r"),)),
    S("put-attached", "gh api -XPUT repos/o/r/pulls/11/merge", 2,
      bases=(("11", "main"),), probes=(("11", "o/r"),)),
    S("put-control-quoted-get", 'gh api -X "GET" repos/o/r/pulls/11/merge', 0,
      bases=(("11", "main"),), probes=()),
    # prnum: a number inside a quoted flag value never stands in for the PR.
    S("prnum-quoted-subject-number", 'gh pr merge --squash --subject "fix 10 things" 11', 2,
      bases=(("10", "dev"), ("11", "main")), probes=(("11", None),)),
    S("prnum-quoted-url-still-read", 'gh pr merge "%s"' % URL11, 2,
      bases=REPO_BASES, probes=(("11", "o/r"),)),
    # repo: the probe asks about the repo the merge acts on.
    S("repo-from-pr-url", "gh pr merge %s --squash" % URL11, 2,
      bases=REPO_BASES, probes=(("11", "o/r"),)),
    S("repo-from-GH_REPO-prefix", "GH_REPO=o/r gh pr merge 11 --squash", 2,
      bases=REPO_BASES, probes=(("11", "o/r"),)),
    S("repo-from-earlier-export", "export GH_REPO=o/r && gh pr merge 11 --squash", 2,
      bases=REPO_BASES, probes=(("11", "o/r"),)),
    S("repo-control-prefix-on-another-command", "GH_REPO=o/r gh pr list && gh pr merge 11 --squash",
      0, bases=REPO_BASES, probes=(("11", None),)),
    S("repo-control-export-after-the-merge", "gh pr merge 11 --squash && export GH_REPO=o/r",
      0, bases=REPO_BASES, probes=(("11", None),)),
    # pushall: --all, --mirror and glob refspecs can update main.
    S("pushall-all", "git push --all origin", 2, bang=_bang("git push --all origin")),
    S("pushall-mirror", "git push --mirror origin", 2, bang=_bang("git push --mirror origin")),
    S("pushall-glob-refspec", "git push origin 'refs/heads/*:refs/heads/*'", 2),
    S("pushall-control-tags", "git push --tags origin", 0),
    S("pushall-control-star-in-an-option", "git push -o 'ci.variable=X=*' origin dev", 0),
    # the `!` line keeps the directory and the steps before the push (nothing ran).
    S("bang-keeps-an-earlier-cd", "cd /x && git push origin main", 2,
      bang=_bang("cd /x && git push origin main")),
    S("bang-prefixes-the-payload-cwd", "cd /x && git push origin main", 2, cwd="/home/user/repo",
      bang=_bang("cd /home/user/repo && cd /x && git push origin main")),
    S("bang-keeps-a-variable", "R=~/other-repo && git -C $R push origin main", 2,
      bang=_bang("R=~/other-repo && git -C $R push origin main")),
    S("bang-quotes-a-windows-cwd", "git push origin main", 2, cwd="C:\\work\\my repo",
      bang=_bang("cd 'C:/work/my repo' && git push origin main")),
    S("bang-drops-what-follows-the-push", "git push origin main && gh pr create --fill", 2,
      bang=_bang("git push origin main")),
    S("bang-leaves-out-an-earlier-merge", "gh pr merge 10 --squash && git push origin main", 2,
      bases=(("10", "dev"),), cwd="/r", bang=_bang("cd /r && git push origin main"),
      err="carries a merge"),
    S("bang-leaves-out-an-earlier-rest-merge",
      "gh api -X PUT repos/o/r/pulls/10/merge && git push origin main", 2,
      bases=(("10", "dev"),), bang=_bang("git push origin main"), err="carries a merge"),
    S("bang-multiline-prefix-gives-the-push-alone",
      "git commit -F - <<'EOF'\nfix\nEOF\ngit push origin main", 2, cwd="/r",
      bang=_bang("cd /r && git push origin main"), err="spans several lines"),
]


def _shape_job(root, shape):
    harness = GateHarness(Path(root) / shape.id)
    harness.bases = dict(shape.bases)
    for target in shape.markers:
        harness.plant(target)
    return harness.run(shape.cmd, cwd=shape.cwd), harness.marker_files()


@pytest.fixture(scope="module")
def review_runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("review")
    shapes = [p.values[0] for p in REVIEW_SHAPES]
    with ThreadPoolExecutor(max_workers=POOL_WORKERS) as pool:
        out = list(pool.map(lambda s: _shape_job(root, s), shapes))
    return {s.id: res for s, res in zip(shapes, out)}


def _probe_view(calls):
    return tuple((c[2], c[c.index("--repo") + 1] if "--repo" in c else None) for c in calls)


@pytest.mark.parametrize("shape", REVIEW_SHAPES)
def test_review_shape(review_runs, shape):
    r, left = review_runs[shape.id]
    if shape.rc == ALLOW:
        assert_allowed(r)
    else:
        assert_blocked(r)
        assert shape.err in r.err, r
    if shape.left is not None:
        assert left == sorted(merge_marker.marker_name(t) for t in shape.left), (left, r)
    if shape.bang is not None:
        assert bang_lines(r.err) == list(shape.bang), r
    if shape.probes is not None:
        assert _probe_view(r.gh_calls) == shape.probes, r


def test_review_shape_ids_are_unique():
    ids = [p.values[0].id for p in REVIEW_SHAPES]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Marker protocol
# ---------------------------------------------------------------------------

NOREPO = "--repo nobody-xyz/never-exists-3027"


def test_armed_marker_allows_exactly_one_merge(gate):
    gate.bases = {"917": "main"}
    marker = gate.plant("917")
    assert_allowed(gate.run("gh pr merge 917 --squash"))
    assert not marker.exists()
    replay = gate.run("gh pr merge 917 --squash")
    assert_blocked(replay)
    assert "reject=path" in replay.err


def test_expired_marker_blocks_and_is_not_consumed(gate):
    gate.bases = {"917": "main"}
    marker = gate.plant("917", age_minutes=11)
    r = gate.run("gh pr merge 917 --squash")
    assert_blocked(r)
    assert "reject=age" in r.err
    assert marker.exists()


def test_marker_inside_the_ttl_allows(gate):
    gate.bases = {"917": "main"}
    gate.plant("917", age_minutes=9)
    assert_allowed(gate.run("gh pr merge 917 --squash"))


def test_future_dated_marker_blocks(gate):
    gate.bases = {"917": "main"}
    gate.plant("917", age_minutes=-5)
    r = gate.run("gh pr merge 917 --squash")
    assert_blocked(r)
    assert "reject=age" in r.err


def test_foreign_session_marker_blocks_and_survives(gate):
    gate.bases = {"917": "main"}
    marker = gate.plant("917", session_id="other-session")
    r = gate.run("gh pr merge 917 --squash", session_id="test-session")
    assert_blocked(r)
    assert "reject=session" in r.err
    assert marker.exists(), "the sibling's marker must not be consumed"


def test_same_session_marker_allows(gate):
    gate.bases = {"917": "main"}
    gate.plant("917", session_id="test-session")
    assert_allowed(gate.run("gh pr merge 917 --squash", session_id="test-session"))


def test_marker_for_another_target_blocks_and_survives(gate):
    gate.bases = {"918": "main"}
    marker = gate.plant("917")
    r = gate.run("gh pr merge 918 --squash")
    assert_blocked(r)
    assert "reject=target" in r.err
    assert marker.exists()


def test_non_main_base_allows_without_a_marker(gate):
    gate.bases = {"917": "dev"}
    r = gate.run("gh pr merge 917 --squash --repo octo/other-repo")
    assert_allowed(r)
    assert r.gh_calls == [["pr", "view", "917", "--json", "baseRefName", "--jq", ".baseRefName",
                           "--repo", "octo/other-repo"]]


def test_base_probe_failure_without_a_marker_blocks(gate):
    r = gate.run("gh pr merge 917 --squash %s" % NOREPO)
    assert_blocked(r)
    assert "gh probe failed" in r.err and "reject=path" in r.err, r
    assert len(r.gh_calls) == 1, "the probe ran and failed -- not skipped"


def test_base_probe_failure_with_a_marker_allows_and_consumes(gate):
    """Number-only binding: the marker for 917 clears a merge naming #917 in any repo."""
    marker = gate.plant("917")
    assert_allowed(gate.run("gh pr merge 917 --squash %s" % NOREPO))
    assert not marker.exists()


def test_nonexistent_pr_blocks(gate):
    r = gate.run("gh pr merge 9999999 --repo octo/home-repo")
    assert_blocked(r)
    assert "PR 9999999" in r.err and "gh probe failed" in r.err, r


def test_a_single_slot_marker_is_not_honoured(gate):
    gate.bases = {"917": "main"}
    (gate.markers / "cc-merge-auth.json").write_text(json.dumps({
        "target": "917", "ts": datetime.now(timezone.utc).isoformat(),
        "note": "mgo 917", "session_id": ""}), encoding="utf-8")
    r = gate.run("gh pr merge 917 --squash")
    assert_blocked(r)
    assert "reject=path" in r.err and "cc-merge-auth.917.json" in r.err, r


def test_rest_merge_marker_is_consumed_and_binds_the_number(gate):
    gate.bases = {"5": "main", "6": "main"}
    marker = gate.plant("5")
    r = gate.run("gh api -X PUT repos/o/r/pulls/6/merge -f merge_method=squash")
    assert_blocked(r)
    assert "reject=target" in r.err and marker.exists(), r
    assert_allowed(gate.run("gh api -X PUT repos/o/r/pulls/5/merge -f merge_method=squash"))
    assert not marker.exists()


def test_merge_block_offers_the_phrase_and_the_bang_line_never_a_self_arm(gate):
    gate.bases = {"917": "main"}
    r = gate.run("gh pr merge 917 --squash")
    assert_blocked(r)
    assert "Do NOT write an authorization marker" in r.err, r
    assert "  mgo 917\n" in r.err, r
    assert "ENTIRE next prompt" in r.err, r
    assert bang_lines(r.err) == ["! <the original blocked command, re-run as-is>"], r
    assert "--target" not in r.err and "--note" not in r.err, r


@pytest.mark.parametrize("cmd", [
    pytest.param("gh pr merge 917 --squash", id="merge"),
    pytest.param("gh api -X PUT repos/o/r/pulls/917/merge", id="rest"),
])
def test_without_plugin_data_a_merge_blocks_even_with_a_marker_on_disk(gate, cmd):
    """G2: no CLAUDE_PLUGIN_DATA, no marker directory, so nothing can clear the merge --
    not even a valid marker sitting where the variable would have pointed."""
    gate.bases = {"917": "main"}
    marker = gate.plant("917")
    r = gate.run(cmd, marker_dir=False)
    assert_blocked(r)
    assert "reject=nodir" in r.err and "CLAUDE_PLUGIN_DATA unset" in r.err, r
    assert marker.exists()


def test_without_plugin_data_a_dev_merge_still_allows(gate):
    gate.bases = {"917": "dev"}
    assert_allowed(gate.run("gh pr merge 917 --squash", marker_dir=False))


# ---------------------------------------------------------------------------
# Payload plumbing, startup and gh resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    pytest.param(b"", id="empty"),
    pytest.param(b"  \n", id="whitespace"),
    pytest.param(b"{not json", id="unparseable"),
    pytest.param(b"null", id="json-null"),
    pytest.param(b"[]", id="json-array"),
    pytest.param(b"\xff\xfe\x00", id="not-utf8"),
])
def test_bad_stdin_blocks(gate, raw):
    """Claude Code always sends a payload: an empty or broken one is broken plumbing."""
    r = gate.run_raw(raw)
    assert_blocked(r)
    assert "empty or unparseable hook payload" in r.err


def test_non_bash_tool_allows(gate):
    assert_allowed(gate.run_raw(payload_bytes("git push origin main", tool_name="Edit")))


def test_import_failure_at_startup_exits_2(gate, tmp_path):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "shlex.py").write_text("raise ImportError('shadowed for the test')\n",
                                     encoding="utf-8")
    r = gate.run("ls", extra_env={"PYTHONPATH": str(shadow)})
    assert r.rc == BLOCK and r.out == "", r
    assert "startup error" in r.err and "shadowed for the test" in r.err, r


def test_a_missing_marker_module_exits_2(gate, tmp_path):
    """The gate imports merge_marker from its own directory; a copy without it fails closed."""
    lone = tmp_path / "lone"
    lone.mkdir()
    copy = lone / "merge_gate.py"
    copy.write_bytes(GATE.read_bytes())
    proc = subprocess.run([sys.executable, str(copy)], input=payload_bytes("ls"),
                          env=gate.env(), capture_output=True, timeout=60)
    assert proc.returncode == BLOCK, proc
    assert b"startup error" in proc.stderr and b"merge_marker" in proc.stderr, proc


def test_python_below_3_10_exits_2(gate):
    code = ("import runpy, sys; sys.version_info = (3, 9, 18, 'final', 0); "
            "runpy.run_path(%r, run_name='__main__')" % str(GATE))
    proc = subprocess.run([sys.executable, "-c", code], input=payload_bytes("ls"),
                          env=gate.env(), capture_output=True, timeout=60)
    assert proc.returncode == BLOCK, proc
    assert b"python >= 3.10 required" in proc.stderr, proc


def test_gh_resolution_order():
    assert gate_mod.GH_FALLBACKS == ("/opt/homebrew/bin/gh", "C:/Program Files/GitHub CLI/gh.exe")


def test_gh_resolution_prefers_path_then_falls_back(monkeypatch, tmp_path):
    first, second = tmp_path / "first-gh", tmp_path / "second-gh"
    second.write_text("", encoding="utf-8")
    monkeypatch.setattr(gate_mod, "GH_FALLBACKS", (str(first), str(second)))
    monkeypatch.setattr(gate_mod.shutil, "which", lambda name: "/on/path/gh")
    assert gate_mod.resolve_gh() == "/on/path/gh"
    monkeypatch.setattr(gate_mod.shutil, "which", lambda name: None)
    assert gate_mod.resolve_gh() == str(second)
    second.unlink()
    assert gate_mod.resolve_gh() is None


@pytest.mark.parametrize("cmd", [
    pytest.param("gh pr merge 917 --squash", id="merge"),
    pytest.param("gh api -X PUT repos/o/r/pulls/917/merge", id="rest"),
])
def test_gh_not_found_blocks_even_with_a_marker(monkeypatch, tmp_path, cmd):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    marker = plant_marker(tmp_path, "917")
    monkeypatch.setattr(gate_mod, "resolve_gh", lambda: None)
    rc, msg = gate_mod.decide(payload_bytes(cmd))
    assert rc == BLOCK
    assert "gh not found" in msg
    assert marker.exists()


# ---------------------------------------------------------------------------
# The probe timeout: a hung gh is a FAILED probe (fail closed), never a hook timeout.
# In-process, with subprocess.run replaced, so no real sleep.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    pytest.param("gh pr merge 917 --squash", id="merge"),
    pytest.param("gh api -X PUT repos/o/r/pulls/917/merge", id="rest"),
])
def test_a_hung_probe_fails_closed_and_its_marker_still_clears_it(monkeypatch, tmp_path, cmd):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setattr(gate_mod, "resolve_gh", lambda: "gh")
    timeouts = []

    def hung(argv, **kw):
        # The bound must be PASSED: without it a hung gh outlives the hook.
        timeouts.append(kw.get("timeout"))
        assert kw.get("timeout") is not None and 0 < kw["timeout"] <= gate_mod.PROBE_TIMEOUT_S
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(gate_mod.subprocess, "run", hung)
    rc, msg = gate_mod.decide(payload_bytes(cmd))
    assert rc == BLOCK, msg
    assert "gh probe failed" in msg and "reject=path" in msg, msg
    assert len(timeouts) == 1, "the probe ran and timed out -- it was not skipped"

    marker = plant_marker(tmp_path, "917")
    assert gate_mod.decide(payload_bytes(cmd)) == (ALLOW, "")
    assert not marker.exists(), "the marker is the way through a failed probe, consumed once"


def test_the_probe_budget_caps_every_probe_of_one_command(monkeypatch):
    ran = []

    def fake_run(argv, **kw):
        ran.append(kw["timeout"])
        return subprocess.CompletedProcess(argv, 0, b"dev\n", b"")

    monkeypatch.setattr(gate_mod.subprocess, "run", fake_run)
    # Budget left: the per-probe bound applies.
    assert gate_mod.probe_base("gh", ["pr", "view", "1"],
                               deadline=time.monotonic() + 10 * gate_mod.PROBE_TIMEOUT_S) == "dev"
    assert ran == [gate_mod.PROBE_TIMEOUT_S]
    # Budget nearly spent: the bound shrinks to what is left.
    assert gate_mod.probe_base("gh", ["pr", "view", "1"], deadline=time.monotonic() + 5) == "dev"
    assert 0 < ran[-1] <= 5
    # Budget spent: a failed probe, and gh never runs.
    assert gate_mod.probe_base("gh", ["pr", "view", "1"], deadline=time.monotonic() - 1) is None
    assert len(ran) == 2
    # Every probe of one command fits inside the 600 s default hook timeout.
    assert gate_mod.PROBE_TIMEOUT_S <= gate_mod.PROBE_BUDGET_S < 600
