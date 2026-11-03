"""Tests for premerge_guard.py -- the pre-merge base-freshness gate (#844).

Includes a documented reproduction of the 2026-06-19 #841->#842 incident: a PR
green against a base that has advanced by >=1 commit MUST be held, proving the
gate would have blocked #841's stale-green merge that reddened main.
"""
import importlib.util
import json
import os
import sys

import pytest
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_GUARD = os.path.join(_HERE, "..", "scripts", "premerge_guard.py")
_spec = importlib.util.spec_from_file_location("premerge_guard", _GUARD)
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

# The workflow name every rollup fixture below carries. The library default is the
# strict UNRESOLVED sentinel (G4), so a test that wants a match names its check.
CI = "agent-os tests"


@pytest.fixture(autouse=True)
def _isolated_merge_lock(tmp_path, monkeypatch):
    """#3237: main() reads and takes the machine-wide merge lease. No test may
    touch the real `<git-common-dir>/eng-flow-merge-main.lock` -- a live lease
    there would flip a GO test to LEASED, and a --resync test would leave one."""
    monkeypatch.setenv("MERGE_LOCK_FILE", str(tmp_path / "merge-main.lock"))
    # The plugin option must come from the test, never the runner's environment.
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_REQUIRED_CHECK", raising=False)


# ---- pure verdict (no gh) --------------------------------------------------

def test_up_to_date_is_go():
    r = pg.evaluate(0)
    assert r["verdict"] == "GO"
    assert r["exit_code"] == pg.GO == 0


def test_behind_by_one_is_stale():
    r = pg.evaluate(1)
    assert r["verdict"] == "STALE"
    assert r["exit_code"] == pg.STALE_HOLD == 3
    assert r["behind_by"] == 1


def test_behind_by_many_is_stale():
    r = pg.evaluate(7)
    assert r["verdict"] == "STALE"
    assert r["exit_code"] == 3


# ---- compare-object parsing ------------------------------------------------

def test_behind_from_compare_parsing():
    assert pg.behind_from_compare({"status": "behind", "behind_by": 3}) == 3
    assert pg.behind_from_compare({"status": "identical", "behind_by": 0}) == 0
    # ahead-only is NOT behind -> trustworthy
    assert pg.behind_from_compare({"status": "ahead", "ahead_by": 5}) == 0
    assert pg.behind_from_compare({}) == 0            # missing key
    assert pg.behind_from_compare(None) == 0          # defensive
    assert pg.behind_from_compare({"behind_by": None}) == 0


# ---- end-to-end resolve via the --gh test seam (monkeypatched) -------------

def test_841_842_repro(monkeypatch):
    """Reproduce 2026-06-19: #841 went green, then ``main`` advanced by one commit
    (the durable-ledger invariant) before #841 merged. Modeled as a PR whose
    branch is behind base by 1. The gate MUST return STALE / exit-3 => #841 would
    have been blocked from its stale-green merge that reddened main."""
    def fake_gh_json(gh, args):
        if args[0] == "pr" and args[1] == "view":
            return {"number": 841, "state": "OPEN",
                    "headRefName": "fix/bg-agent-max-backstop", "baseRefName": "main"}
        if args[0] == "api":  # compare base...head
            return {"status": "behind", "behind_by": 1, "ahead_by": 2}
        raise AssertionError("unexpected gh call: %r" % (args,))

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.resolve(841, repo="octo/home-repo", gh="gh")
    assert res["verdict"] == "STALE", "the #841->#842 stale-green merge must be blocked"
    assert res["exit_code"] == 3
    assert res["behind_by"] == 1


def test_resolve_up_to_date_merges(monkeypatch):
    """Happy path: a branch that contains all of base (behind_by 0) is GO."""
    def fake_gh_json(gh, args):
        if args[0] == "pr":
            return {"number": 900, "state": "OPEN",
                    "headRefName": "feat/x", "baseRefName": "main"}
        return {"status": "ahead", "behind_by": 0, "ahead_by": 3}

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.resolve(900, repo="o/r", gh="gh")
    assert res["verdict"] == "GO"
    assert res["exit_code"] == 0


def test_already_merged_pr_is_noop_never_go(monkeypatch):
    """#2748: a non-OPEN PR is still never GATED (nothing to block, and resolve
    must short-circuit before the compare call) -- but it is not GO either. GO
    is what every upstream consumer reads as "merge this", and for five hours
    on 2026-09-11 an already-merged #2737 printed the same GO line as an open
    PR: three `Mgo` round-trips, one one-shot marker armed for a no-op."""
    def fake_gh_json(gh, args):
        assert not (args[0] == "api"), "must not compare a non-OPEN PR"
        return {"number": 5, "state": "MERGED", "headRefName": "b", "baseRefName": "main",
                "mergedAt": "2026-09-11T14:14:23Z",
                "mergeCommit": {"oid": "f229afac3aeea692cdfe9905e38d4097b9e15b8d"},
                "closedAt": "2026-09-11T14:14:23Z"}

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.resolve(5, repo="o/r", gh="gh")
    assert res["verdict"] == "ALREADY_MERGED"
    assert res["exit_code"] == pg.NOOP
    assert res["exit_code"] not in (pg.GO, pg.STALE_HOLD, pg.UNDETERMINED)
    assert res["state"] == "MERGED"
    assert res["merged_at"] == "2026-09-11T14:14:23Z"
    assert res["merge_commit"] == "f229afac3aeea692cdfe9905e38d4097b9e15b8d"


def test_already_closed_unmerged_pr_is_noop_never_go(monkeypatch):
    """CLOSED-without-merge is the other non-OPEN state: same distinct exit, its
    own verdict, no merge fields to fabricate."""
    def fake_gh_json(gh, args):
        assert not (args[0] == "api"), "must not compare a non-OPEN PR"
        return {"number": 6, "state": "CLOSED", "headRefName": "b", "baseRefName": "main",
                "mergedAt": None, "mergeCommit": None,
                "closedAt": "2026-09-10T08:00:00Z"}

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.resolve(6, repo="o/r", gh="gh")
    assert res["verdict"] == "ALREADY_CLOSED"
    assert res["exit_code"] == pg.NOOP
    assert res["merged_at"] is None
    assert res["merge_commit"] is None
    assert res["closed_at"] == "2026-09-10T08:00:00Z"


def test_noop_exit_code_is_distinct_and_documented():
    """The distinct code must collide with nothing this guard already emits, and
    must not reuse dup_scan's 4 (= UNDETERMINED there): the two tools are read
    side by side, and CLAUDE.md quotes dup_scan's table verbatim."""
    assert pg.NOOP == 5
    assert pg.NOOP not in (pg.GO, pg.STALE_HOLD, pg.DUP_HOLD, pg.GREEN_HOLD,
                           pg.UNDETERMINED)
    assert "5 = NOOP" in pg.__doc__


# ---- #2294: every verdict must name the repo it actually inspected ----------
# A bare `mgo <N>` cannot carry a repo, and the hook runs this guard with
# cwd=PROJECT_ROOT, so `gh` infers home-repo for EVERY pr number. When the
# number also exists there, the checks run against an unrelated PR. The verdict
# must therefore say which repo it read, and whether that repo was guessed.


def test_non_open_verdict_names_the_repo(monkeypatch):
    """The sharp edge: a wrong-repo number that is already MERGED short-circuits
    immediately. Since #2748 that is a NOOP (exit 5), so preflight_auth no longer
    arms on it -- but the verdict must still name the repo it inspected, or an
    operator cannot tell "your PR is merged" from "a same-numbered PR in another
    repo is merged"."""
    def fake_gh_json(gh, args):
        if args[0] == "repo":
            return {"nameWithOwner": "octo/home-repo"}
        return {"number": 655, "state": "MERGED", "headRefName": "b",
                "baseRefName": "main"}

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.resolve(655, gh="gh")            # no repo= -> inferred, as the hook calls it
    assert res["verdict"] == "ALREADY_MERGED"
    assert res["exit_code"] == pg.NOOP
    assert res["note"] == "PR not OPEN -- nothing to merge"
    assert res["repo"] == "octo/home-repo"
    assert res["repo_inferred"] is True


def test_resolve_marks_inferred_repo(monkeypatch):
    """repo=None means `gh` guessed it from cwd -- the verdict must say so."""
    def fake_gh_json(gh, args):
        if args[0] == "repo":
            return {"nameWithOwner": "octo/home-repo"}
        if args[0] == "api":
            return {"status": "identical", "behind_by": 0}
        return {"number": 658, "state": "OPEN", "headRefName": "h",
                "baseRefName": "main"}

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.resolve(658, gh="gh")
    assert res["repo"] == "octo/home-repo"
    assert res["repo_inferred"] is True


def test_resolve_explicit_repo_is_not_marked_inferred(monkeypatch):
    """An explicit --repo is authoritative and must NOT be flagged as guessed."""
    def fake_gh_json(gh, args):
        assert args[0] != "repo", "must not re-infer when --repo was given"
        if args[0] == "api":
            return {"status": "identical", "behind_by": 0}
        return {"number": 658, "state": "OPEN", "headRefName": "h",
                "baseRefName": "main"}

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.resolve(658, repo="octo/other-repo", gh="gh")
    assert res["repo"] == "octo/other-repo"
    assert res["repo_inferred"] is False


# ---- #2785: the PR-metadata read falls back to REST when GraphQL is spent -----
#
# 2026-09-15 17:2x IL, `Mgo 2783`: the guard returned UNDETERMINED exit 2 with
# "gh pr view 2783 --json … failed: GraphQL: API rate limit already exceeded" on a
# PR that was open, clean, current and green over REST. #2583 gave the CI-rollup
# leg a REST fallback; the metadata read one leg EARLIER (`pr view`) and the
# `gh repo view` repo-inference had none, so an exhausted bucket blinded the gate
# before any of #2583's work could run.

_GQL_RATE_LIMIT = ("gh pr view 2783 --json number,state failed: GraphQL: API rate limit "
                   "already exceeded for user ID 33196704.")


def _rest_pull(state="open", merged=False, head="fix/2782-x", base="main",
               merged_at=None, merge_sha=None, closed_at=None):
    """The `gh api repos/o/r/pulls/N` shape (REST), as GitHub serves it."""
    return {"number": 2783, "state": state, "merged": merged,
            "head": {"ref": head}, "base": {"ref": base},
            "merged_at": merged_at, "merge_commit_sha": merge_sha or "deadbeef",
            "closed_at": closed_at}


def _gql_dead_rest_alive(pull, compare=None):
    """`pr view` / `repo view` raise the live rate-limit text; REST answers."""
    calls = []

    def fake_gh_json(gh, args):
        calls.append(list(args))
        if args[0] in ("pr", "repo"):
            raise RuntimeError(_GQL_RATE_LIMIT)
        if args[0] == "api" and "/pulls/" in args[1]:
            return pull
        if args[0] == "api" and "/compare/" in args[1]:
            return compare or {"status": "identical", "behind_by": 0}
        raise AssertionError("unexpected gh call: %r" % (args,))
    return fake_gh_json, calls


def test_resolve_reads_pr_over_rest_when_graphql_is_rate_limited(monkeypatch):
    """The live 2026-09-15 shape: GraphQL dead, REST alive, PR open + current -> GO,
    with the transport named so the reading is attributable."""
    fake, calls = _gql_dead_rest_alive(_rest_pull())
    monkeypatch.setattr(pg, "_gh_json", fake)
    res = pg.resolve(2783, repo="octo/home-repo", gh="gh")
    assert res["verdict"] == "GO" and res["exit_code"] == 0, res
    assert res["state"] == "OPEN" and res["head"] == "fix/2782-x" and res["base"] == "main"
    assert res["transport"] == "rest"
    assert ["api", "repos/octo/home-repo/pulls/2783"] in calls


def test_resolve_rest_fallback_infers_repo_over_git_not_gh_repo_view(monkeypatch):
    """No --repo (the hook's shape): the repo comes from `_rest_nwo` (git origin),
    never `gh repo view` -- that is GraphQL too, and dead in exactly this case."""
    fake, calls = _gql_dead_rest_alive(_rest_pull())
    monkeypatch.setattr(pg, "_gh_json", fake)
    monkeypatch.setattr(pg, "_rest_nwo", lambda repo, gh="gh": repo or "octo/home-repo")
    res = pg.resolve(2783, gh="gh")
    assert res["verdict"] == "GO"
    assert res["repo"] == "octo/home-repo" and res["repo_inferred"] is True
    assert not any(c[:2] == ["repo", "view"] for c in calls), calls


def test_resolve_rest_fallback_reports_already_merged(monkeypatch):
    """The #2748 branch is reachable over REST: merged=true -> ALREADY_MERGED / NOOP,
    with the merge sha and time carried, and `merge_commit_sha` NOT read as a merge
    when merged is false (GitHub fills it with a test-merge sha on open PRs)."""
    fake, _ = _gql_dead_rest_alive(_rest_pull(state="closed", merged=True,
                                              merged_at="2026-09-15T14:50:00Z",
                                              merge_sha="8f27dbd78bf24eb0414a30f318b258251013ed8e",
                                              closed_at="2026-09-15T14:50:00Z"))
    monkeypatch.setattr(pg, "_gh_json", fake)
    res = pg.resolve(2783, repo="o/r", gh="gh")
    assert res["verdict"] == "ALREADY_MERGED" and res["exit_code"] == pg.NOOP
    assert res["merge_commit"] == "8f27dbd78bf24eb0414a30f318b258251013ed8e"
    assert res["merged_at"] == "2026-09-15T14:50:00Z"
    assert res["transport"] == "rest"
    # closed-unmerged is CLOSED, not MERGED, whatever merge_commit_sha says
    fake, _ = _gql_dead_rest_alive(_rest_pull(state="closed", merged=False,
                                              closed_at="2026-09-15T14:50:00Z"))
    monkeypatch.setattr(pg, "_gh_json", fake)
    res = pg.resolve(2783, repo="o/r", gh="gh")
    assert res["verdict"] == "ALREADY_CLOSED" and res["merge_commit"] is None


def test_resolve_both_transports_dead_is_undetermined_naming_both(monkeypatch):
    """Fail-closed contract, one leg earlier than #2583 measured it: only when
    GraphQL AND REST both fail does resolve() raise -- and the error names both."""
    def fake_gh_json(gh, args):
        if args[0] in ("pr", "repo"):
            raise RuntimeError(_GQL_RATE_LIMIT)
        raise RuntimeError("gh api repos/o/r/pulls/2783 failed: HTTP 502")

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    with pytest.raises(RuntimeError) as ei:
        pg.resolve(2783, repo="o/r", gh="gh")
    msg = str(ei.value)
    assert "rate limit" in msg and "502" in msg, msg


def test_resolve_rest_fallback_without_any_repo_is_undetermined(monkeypatch):
    """No --repo and no readable origin: the fallback cannot name a REST path and
    must say so rather than guess (never `gh repo view`, never a silent GO)."""
    fake, _ = _gql_dead_rest_alive(_rest_pull())
    monkeypatch.setattr(pg, "_gh_json", fake)
    monkeypatch.setattr(pg, "_rest_nwo", lambda repo, gh="gh": None)
    with pytest.raises(RuntimeError) as ei:
        pg.resolve(2783, gh="gh")
    assert "owner/repo" in str(ei.value) and "rate limit" in str(ei.value)


def test_resolve_graphql_path_is_unchanged_and_names_its_transport(monkeypatch):
    """GraphQL healthy -> exactly the old reads, no REST pulls call, transport=graphql."""
    calls = []

    def fake_gh_json(gh, args):
        calls.append(list(args))
        if args[0] == "pr":
            return {"number": 2783, "state": "OPEN", "headRefName": "h", "baseRefName": "main"}
        if args[0] == "api" and "/compare/" in args[1]:
            return {"status": "identical", "behind_by": 0}
        raise AssertionError("unexpected gh call: %r" % (args,))

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.resolve(2783, repo="o/r", gh="gh")
    assert res["verdict"] == "GO" and res["transport"] == "graphql"
    assert not any("/pulls/" in c[1] for c in calls if c[0] == "api")


# ---- merge-time duplicate-linkage gate (#844 follow-up: branch-time vs --------
# ---- merge-time blind spot; #860/#862 class) ---------------------------------

def test_closing_issues_only_closing_keywords():
    # closing keywords (any case) -> captured
    assert pg._closing_issues("Closes #844") == {844}
    assert pg._closing_issues("fixes #12 and Resolved #34") == {12, 34}
    assert pg._closing_issues("FIX #7") == {7}
    # bare refs / non-closing phrasing -> NOT a closing signal
    assert pg._closing_issues("feat(ci): merge_group (#844)") == set()
    assert pg._closing_issues("Part of #5, refs #6") == set()
    assert pg._closing_issues("") == set()
    assert pg._closing_issues(None) == set()
    # same-line extra whitespace still counts (not a newline, so still a real closer)
    assert pg._closing_issues("Closes  #1863") == {1863}


def test_closing_issues_negated_heading_across_blank_line_not_a_closer():
    # #1869 false DUP-HOLD repro: PR #1864's own "## What this does NOT close"
    # section, followed by a blank line and a bare "#1863 stays open" paragraph.
    # \s+ used to span the blank line and match "close" + "#1863" as a genuine
    # closer, despite the NOT and the paragraph break; [ \t]+ keeps the keyword
    # and its ref on the same line, so this must NOT be a closing signal.
    text = (
        "## What this does NOT close\n\n"
        "#1863 stays **open**. Gap 2's AC demands the chat alarm be "
        "verified by an actual send."
    )
    assert pg._closing_issues(text) == set()


# ---- #2976: a keyword QUOTED in code is not a closer -------------------------
# A keyword inside an inline code span or a fenced block is literal text, not a
# closer (GitHub left #2960's code-span one unlinked). The guard used to scan the
# raw body, so a merged PR that merely quoted one false-DUPLICATE-held the real fix.

def test_closing_issues_inline_code_span_not_a_closer():
    assert pg._closing_issues("its `Closes #5` keyword left the history") == set()


def test_closing_issues_fenced_block_not_a_closer():
    text = (
        "Example of the trailer:\n\n"
        "```text\n"
        "Closes #5\n"
        "```\n"
        "~~~\n"
        "Fixes #6\n"
        "~~~\n"
    )
    assert pg._closing_issues(text) == set()
    # a body edited in the web UI arrives with CRLF line ends
    assert pg._closing_issues(text.replace("\n", "\r\n")) == set()


def test_closing_issues_bare_keyword_still_a_closer_control():
    # the control: stripping code must not blind the guard to a real closer
    # after a closed fence, and must not fuse a keyword with a ref that only a
    # code span sat between
    assert pg._closing_issues("Closes #5") == {5}
    assert pg._closing_issues("```\nnote\n```\nFixes #7\n") == {7}
    assert pg._closing_issues("Closes `x` #8") == set()
    # an UNCLOSED fence is not stripped: counting a keyword is the fail-safe
    # direction (HOLD), hiding one is not (false GO)
    assert pg._closing_issues("```\nResolves #9") == {9}


def test_closing_issues_mixed_bare_and_code_span_keeps_only_bare():
    text = "Closes #2959\n\nIts `Closes #2963` keyword was force-pushed off."
    assert pg._closing_issues(text) == {2959}


def test_closing_issues_double_backtick_span_2960_repro():
    # #2976 repro: PR #2960's body quoted the keyword in a DOUBLE-backtick span
    # wrapping a backtick-quoted keyword (CommonMark's way to show backticks).
    # GitHub linked only the bare line-1 closer; the guard took both.
    text = (
        "Closes #2959\n\n"
        "... were force-pushed off the branch so its `` `Closes #2963` `` "
        "keyword left the PR history ..."
    )
    assert pg._closing_issues(text) == {2959}


def _make_fake(me, others):
    """Build a fake _gh_json: `pr view <pr>` -> me; `pr list` -> others."""
    def fake_gh_json(gh, args):
        if args[0] == "pr" and args[1] == "view":
            return me
        if args[0] == "pr" and args[1] == "list":
            return others
        raise AssertionError("unexpected gh call: %r" % (args,))
    return fake_gh_json


def test_860_862_repro_merge_time_duplicate(monkeypatch):
    """#860 (mine) closes #844; #862 (concurrent) MERGED closing #844 inside the
    build window. The merge-time gate MUST HOLD -- this is the exact class the
    branch-time prebranch check could not see (#862 did not exist at branch)."""
    me = {"number": 860, "state": "OPEN",
          "title": "feat(eng-flow): pre-merge base-freshness gate (#844)",
          "body": "Closes #844"}
    others = [
        me,  # self is excluded
        {"number": 862, "state": "MERGED",
         "title": "feat(ci): merge_group trigger (#844)", "body": "Closes #844"},
    ]
    monkeypatch.setattr(pg, "_gh_json", _make_fake(me, others))
    res = pg.merge_time_duplicate(860, repo="octo/home-repo", gh="gh")
    assert res["verdict"] == "DUPLICATE"
    assert res["exit_code"] == pg.DUP_HOLD == 3
    assert res["duplicates"][0]["number"] == 862
    assert res["issues"] == [844]


def test_merge_time_no_other_closer_is_go(monkeypatch):
    me = {"number": 900, "state": "OPEN", "title": "feat: x (#10)", "body": "Closes #10"}
    others = [me, {"number": 901, "state": "OPEN",
                   "title": "feat: y", "body": "Closes #99"}]
    monkeypatch.setattr(pg, "_gh_json", _make_fake(me, others))
    res = pg.merge_time_duplicate(900, repo="o/r", gh="gh")
    assert res["verdict"] == "GO"
    assert res["exit_code"] == 0


def test_merge_time_only_self_is_go(monkeypatch):
    me = {"number": 5, "state": "OPEN", "title": "feat", "body": "Fixes #5"}
    monkeypatch.setattr(pg, "_gh_json", _make_fake(me, [me]))
    res = pg.merge_time_duplicate(5, repo="o/r", gh="gh")
    assert res["verdict"] == "GO"


def test_merge_time_non_closing_ref_ignored(monkeypatch):
    """Another PR that only *references* the issue (`Part of #N`) is NOT a
    duplicate closer -- must not HOLD."""
    me = {"number": 5, "state": "OPEN", "title": "feat", "body": "Closes #844"}
    others = [me, {"number": 6, "state": "OPEN",
                   "title": "chore (#844)", "body": "Part of #844, refs #844"}]
    monkeypatch.setattr(pg, "_gh_json", _make_fake(me, others))
    res = pg.merge_time_duplicate(5, repo="o/r", gh="gh")
    assert res["verdict"] == "GO"


def test_merge_time_closed_unmerged_other_ignored(monkeypatch):
    """A CLOSED-but-not-merged duplicate is dead -- it never landed, so it is not
    a live duplicate and must not HOLD."""
    me = {"number": 5, "state": "OPEN", "title": "feat", "body": "Closes #844"}
    others = [me, {"number": 6, "state": "CLOSED",
                   "title": "abandoned", "body": "Closes #844"}]
    monkeypatch.setattr(pg, "_gh_json", _make_fake(me, others))
    res = pg.merge_time_duplicate(5, repo="o/r", gh="gh")
    assert res["verdict"] == "GO"


def test_merge_time_pr_closes_nothing_is_go(monkeypatch):
    """A PR that closes no issue (e.g. a pure refactor) skips the dup check."""
    me = {"number": 5, "state": "OPEN", "title": "refactor", "body": "no issue ref"}
    def fake_gh_json(gh, args):
        assert not (args[0] == "pr" and args[1] == "list"), \
            "must not list PRs when this PR closes nothing"
        return me
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.merge_time_duplicate(5, repo="o/r", gh="gh")
    assert res["verdict"] == "GO"
    assert res.get("note") == "closes no issue"


def test_merge_time_non_open_pr_is_go(monkeypatch):
    """A non-OPEN PR is never gated (mirrors the base-freshness short-circuit)."""
    me = {"number": 5, "state": "MERGED", "title": "feat", "body": "Closes #844"}
    def fake_gh_json(gh, args):
        assert not (args[0] == "pr" and args[1] == "list"), "must not list for non-OPEN PR"
        return me
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.merge_time_duplicate(5, repo="o/r", gh="gh")
    assert res["verdict"] == "GO"


# ---- file-overlap ADVISORY (#877: the linkage gate misses different-issue ----
# ---- parallel dups; base-freshness is the only incidental catch) -------------
# Fixed clock so the build-window recency tests are deterministic (no real now()).
_WINDOW_NOW = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)


def test_pr_paths_extracts_paths():
    assert pg._pr_paths({"files": [{"path": "a.py"}, {"path": "b.py"}]}) == {"a.py", "b.py"}
    assert pg._pr_paths({"files": []}) == set()
    assert pg._pr_paths({}) == set()
    assert pg._pr_paths({"files": [{"additions": 1}]}) == set()  # no path key -> dropped


def test_873_874_repro_file_overlap_advisory(monkeypatch):
    """#874 (mine, OPEN, closes #876) and #873 (concurrent, MERGED in-window, closes
    #875) both touch dashboard_producer.py but close DIFFERENT issues -- so the
    merge-time *linkage* gate cannot see them. The file-overlap advisory MUST surface
    #873, yet stay GO/exit-0 (it is a note, never a HOLD). This is the exact 2026-06-19
    second occurrence #877 was filed for."""
    F = "app/dashboard/dashboard_producer.py"
    me = {"number": 874, "state": "OPEN", "title": "fix dashboard time-bomb",
          "body": "Closes #876", "files": [{"path": F}]}
    others = [
        me,  # self excluded
        {"number": 873, "state": "MERGED", "title": "fix(dashboard): skip dir mtimes",
         "url": "https://github.com/o/r/pull/873", "body": "Closes #875",
         "mergedAt": "2026-06-19T11:58:58Z", "files": [{"path": F}]},
    ]
    monkeypatch.setattr(pg, "_gh_json", _make_fake(me, others))
    res = pg.file_overlap_advisory(874, repo="o/r", gh="gh", now=_WINDOW_NOW)
    assert res["verdict"] == "GO"
    assert res["exit_code"] == pg.GO == 0          # ADVISORY never holds
    assert res["advisory"] is True
    assert res["overlaps"][0]["number"] == 873
    assert F in res["overlaps"][0]["files"]


def test_file_overlap_advisory_never_holds(monkeypatch):
    """Even with a confirmed overlap, the exit code stays GO (0) -- the advisory is
    informational and must never change merge eligibility (guards the regression
    where an advisory leaks into the exit-3 HOLD path)."""
    me = {"number": 10, "state": "OPEN", "files": [{"path": "a.py"}]}
    others = [me, {"number": 12, "state": "OPEN", "title": "concurrent", "url": "u",
                   "files": [{"path": "a.py"}, {"path": "c.py"}]}]
    monkeypatch.setattr(pg, "_gh_json", _make_fake(me, others))
    res = pg.file_overlap_advisory(10, repo="o/r", gh="gh", now=_WINDOW_NOW)
    assert res["advisory"] is True
    assert res["exit_code"] == 0
    assert res["overlaps"][0]["number"] == 12
    assert res["overlaps"][0]["files"] == ["a.py"]  # only the shared path, sorted


def test_file_overlap_no_shared_files_is_go(monkeypatch):
    me = {"number": 10, "state": "OPEN", "files": [{"path": "a.py"}]}
    others = [me, {"number": 11, "state": "OPEN", "title": "x", "url": "u",
                   "files": [{"path": "b.py"}]}]
    monkeypatch.setattr(pg, "_gh_json", _make_fake(me, others))
    res = pg.file_overlap_advisory(10, repo="o/r", gh="gh", now=_WINDOW_NOW)
    assert res["verdict"] == "GO" and res["exit_code"] == 0
    assert res["overlaps"] == []
    assert res["advisory"] is False


def test_file_overlap_old_merged_excluded(monkeypatch):
    """A PR MERGED *before* the build window touching the same file is normal history
    (every PR editing premerge_guard.py would match) -- exclude it to keep the signal
    low-noise. Only build-window landings (like #873-during-#874) count."""
    me = {"number": 10, "state": "OPEN", "files": [{"path": "a.py"}]}
    others = [me, {"number": 9, "state": "MERGED", "title": "old", "url": "u",
                   "mergedAt": "2026-06-17T00:00:00Z",  # >24h before _WINDOW_NOW
                   "files": [{"path": "a.py"}]}]
    monkeypatch.setattr(pg, "_gh_json", _make_fake(me, others))
    res = pg.file_overlap_advisory(10, repo="o/r", gh="gh", now=_WINDOW_NOW,
                                   window_hours=24)
    assert res["overlaps"] == []


def test_file_overlap_closed_unmerged_excluded(monkeypatch):
    """A CLOSED-but-unmerged PR never landed -> not live overlap."""
    me = {"number": 10, "state": "OPEN", "files": [{"path": "a.py"}]}
    others = [me, {"number": 8, "state": "CLOSED", "title": "abandoned", "url": "u",
                   "files": [{"path": "a.py"}]}]
    monkeypatch.setattr(pg, "_gh_json", _make_fake(me, others))
    res = pg.file_overlap_advisory(10, repo="o/r", gh="gh", now=_WINDOW_NOW)
    assert res["overlaps"] == []


def test_file_overlap_pr_no_files_is_go(monkeypatch):
    """A PR with no changed files (defensive) skips the list call entirely."""
    me = {"number": 10, "state": "OPEN", "files": []}
    def fake_gh_json(gh, args):
        assert not (args[0] == "pr" and args[1] == "list"), \
            "must not list PRs when this PR has no files"
        return me
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.file_overlap_advisory(10, repo="o/r", gh="gh", now=_WINDOW_NOW)
    assert res["verdict"] == "GO"
    assert res.get("note") == "no files"


def test_file_overlap_non_open_pr_is_go(monkeypatch):
    """A non-OPEN PR is never advised on (mirrors the other gates' short-circuit)."""
    me = {"number": 10, "state": "MERGED", "files": [{"path": "a.py"}]}
    def fake_gh_json(gh, args):
        assert not (args[0] == "pr" and args[1] == "list"), "must not list for non-OPEN PR"
        return me
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    res = pg.file_overlap_advisory(10, repo="o/r", gh="gh", now=_WINDOW_NOW)
    assert res["verdict"] == "GO"


# ---- CI-green precondition gate (#1098) --------------------------------------
# Real statusCheckRollup shape grounded from PR #1105: our CI is a GitHub Actions
# workflow named "agent-os tests" whose CheckRun entries carry it in workflowName
# (job names are "test" + the PR-skipped "advance-deploy-ref").

# The exact rollup #1105 returned when green: the test job SUCCESS + the
# advance-deploy-ref job SKIPPED-on-PR, both under workflowName "agent-os tests".
_ROLLUP_GREEN = [
    {"__typename": "CheckRun", "workflowName": "agent-os tests", "name": "test",
     "status": "COMPLETED", "conclusion": "SUCCESS", "context": None, "state": None},
    {"__typename": "CheckRun", "workflowName": "agent-os tests",
     "name": "advance-deploy-ref", "status": "COMPLETED", "conclusion": "SKIPPED",
     "context": None, "state": None},
]
_ROLLUP_PENDING = [
    {"__typename": "CheckRun", "workflowName": "agent-os tests", "name": "test",
     "status": "IN_PROGRESS", "conclusion": None, "context": None, "state": None},
]
_ROLLUP_RED = [
    {"__typename": "CheckRun", "workflowName": "agent-os tests", "name": "test",
     "status": "COMPLETED", "conclusion": "FAILURE", "context": None, "state": None},
]


class _FakeClock:
    """Deterministic clock+sleep so green_gate's poll loop never really waits."""
    def __init__(self, start=1000.0, step=1.0):
        self.t, self.step = start, step
    def now(self):
        self.t += self.step
        return self.t
    def sleep(self, secs):
        self.t += max(0.0, secs)


# ---- _match_check: workflowName is the matchable field ----
def test_match_check_on_workflow_name():
    c = {"workflowName": "agent-os tests", "name": "test"}
    assert pg._match_check(c, "agent-os tests") is True
    # the JOB name "test" alone must NOT match the required workflow string
    assert pg._match_check({"workflowName": "other", "name": "test"},
                           "agent-os tests") is False


def test_match_check_legacy_statuscontext():
    assert pg._match_check({"context": "agent-os tests", "__typename": "StatusContext"},
                           "agent-os tests") is True


def test_match_check_empty_required_is_false():
    assert pg._match_check({"workflowName": "agent-os tests"}, "") is False


# ---- _check_outcome: SKIPPED is not failure; pending vs success vs failure ----
def test_check_outcome_success():
    assert pg._check_outcome(_ROLLUP_GREEN[0]) == "success"


def test_check_outcome_skipped_is_not_failure():
    assert pg._check_outcome(_ROLLUP_GREEN[1]) == "skipped"


def test_check_outcome_pending():
    assert pg._check_outcome(_ROLLUP_PENDING[0]) == "pending"


def test_check_outcome_failure():
    assert pg._check_outcome(_ROLLUP_RED[0]) == "failure"


def test_check_outcome_statuscontext():
    assert pg._check_outcome({"__typename": "StatusContext", "context": "x",
                              "state": "SUCCESS"}) == "success"
    assert pg._check_outcome({"__typename": "StatusContext", "context": "x",
                              "state": "PENDING"}) == "pending"
    assert pg._check_outcome({"__typename": "StatusContext", "context": "x",
                              "state": "ERROR"}) == "failure"


# ---- evaluate_checks: the real green rollup is 'success' despite SKIPPED sibling ----
def test_evaluate_checks_green_ignores_skipped_sibling():
    """The #1105 green shape: test SUCCESS + advance-deploy-ref SKIPPED => success.
    Regression guard: the skipped PR-only deploy job must NOT false-fail the gate."""
    assert pg.evaluate_checks(_ROLLUP_GREEN, "agent-os tests") == "success"


def test_evaluate_checks_pending():
    assert pg.evaluate_checks(_ROLLUP_PENDING, "agent-os tests") == "pending"


def test_evaluate_checks_failure():
    assert pg.evaluate_checks(_ROLLUP_RED, "agent-os tests") == "failure"


def test_evaluate_checks_failure_beats_pending_and_success():
    mixed = _ROLLUP_GREEN + _ROLLUP_PENDING + _ROLLUP_RED
    assert pg.evaluate_checks(mixed, "agent-os tests") == "failure"


def test_evaluate_checks_absent_when_no_match():
    assert pg.evaluate_checks([{"workflowName": "lint", "name": "lint",
                                "status": "COMPLETED", "conclusion": "SUCCESS"}],
                              "agent-os tests") == "absent"
    assert pg.evaluate_checks([], "agent-os tests") == "absent"
    assert pg.evaluate_checks(None, "agent-os tests") == "absent"


def test_evaluate_checks_only_skipped_is_absent():
    only_skipped = [_ROLLUP_GREEN[1]]  # just the SKIPPED advance-deploy-ref
    assert pg.evaluate_checks(only_skipped, "agent-os tests") == "absent"


# ---- green_gate: GO on green, HOLD on red, wait-through-pending ----
def test_green_gate_go_when_green(monkeypatch):
    monkeypatch.setattr(pg, "fetch_check_rollup", lambda *a, **k: _ROLLUP_GREEN)
    res = pg.green_gate(1105, repo="o/r", gh="gh", required=CI)
    assert res["verdict"] == "GO"
    assert res["exit_code"] == pg.GO == 0
    assert res["ci"] == "success"


def test_green_gate_holds_on_confirmed_red(monkeypatch):
    """A confirmed FAILURE HOLDs immediately (no waiting on a known-red PR)."""
    monkeypatch.setattr(pg, "fetch_check_rollup", lambda *a, **k: _ROLLUP_RED)
    res = pg.green_gate(99, repo="o/r", gh="gh", wait_green=9999, required=CI)
    assert res["verdict"] == "NOT_GREEN"
    assert res["exit_code"] == pg.GREEN_HOLD == 3
    assert res["ci"] == "failure"


def test_green_gate_holds_on_pending_with_no_wait(monkeypatch):
    """--wait-green 0: pending blocks immediately (the standalone-tool default)."""
    monkeypatch.setattr(pg, "fetch_check_rollup", lambda *a, **k: _ROLLUP_PENDING)
    res = pg.green_gate(7, repo="o/r", gh="gh", wait_green=0, required=CI)
    assert res["verdict"] == "NOT_GREEN"
    assert res["exit_code"] == 3
    assert res["ci"] == "pending"


def test_green_gate_waits_through_pending_then_passes(monkeypatch):
    """The eng-flow case: CI is PENDING at merge time, then turns green. The gate
    must POLL through pending and ultimately GO -- a bare assert would wedge every
    --auto-merge run (eng-flow merges before CI completes)."""
    seq = [_ROLLUP_PENDING, _ROLLUP_PENDING, _ROLLUP_GREEN]
    calls = {"i": 0}
    def fake_fetch(*a, **k):
        r = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        return r
    monkeypatch.setattr(pg, "fetch_check_rollup", fake_fetch)
    fc = _FakeClock(step=1.0)
    res = pg.green_gate(7, repo="o/r", gh="gh", wait_green=1000,
                        sleep=fc.sleep, now=fc.now, required=CI)
    assert res["verdict"] == "GO"
    assert calls["i"] >= 3  # polled through both pending reads


def test_green_gate_holds_when_pending_past_deadline(monkeypatch):
    """Pending that never resolves within --wait-green => HOLD (not an infinite wait)."""
    monkeypatch.setattr(pg, "fetch_check_rollup", lambda *a, **k: _ROLLUP_PENDING)
    fc = _FakeClock(step=1.0)
    res = pg.green_gate(7, repo="o/r", gh="gh", wait_green=3,
                        sleep=fc.sleep, now=fc.now, required=CI)
    assert res["verdict"] == "NOT_GREEN"
    assert res["exit_code"] == 3


# ---- main() integration: green HOLD overrides GO; probe failure is fail-open ----
def _stub_green_path(monkeypatch, green_result=None, green_raises=False):
    """Stub resolve/dup/overlap to GO so main()'s verdict is driven by green_gate."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "behind_by": 0,
                         "pr": k.get("pr", 1), "base": "main", "state": "OPEN"})
    monkeypatch.setattr(pg, "merge_time_duplicate", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0})
    monkeypatch.setattr(pg, "file_overlap_advisory", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "overlaps": []})
    # #1914: main() now derives the required-check name via resolve_required_check
    # before calling green_gate. These tests care about the green-gate WIRING, not
    # the derivation, so pin it to the constant -- otherwise an un-mocked
    # resolve_required_check would issue a real `gh api .../actions/workflows` call.
    monkeypatch.setattr(pg, "resolve_required_check",
                        lambda *a, **k: CI)
    if green_raises:
        def boom(*a, **k):
            raise RuntimeError("gh down")
        monkeypatch.setattr(pg, "green_gate", boom)
    else:
        monkeypatch.setattr(pg, "green_gate", lambda *a, **k: green_result)


def test_main_holds_when_ci_not_green(monkeypatch):
    _stub_green_path(monkeypatch, green_result={
        "verdict": "NOT_GREEN", "exit_code": pg.GREEN_HOLD, "pr": 5,
        "check": "agent-os tests", "ci": "failure"})
    assert pg.main(["--pr", "5", "--repo", "o/r"]) == 3


def test_main_green_probe_failure_fails_closed(monkeypatch):
    """#1736 reversal: a green-probe exception now HOLDs (fail CLOSED).

    Previously this asserted fail-OPEN -- an exception was swallowed and the
    verdict stayed GO. That is the exact defect class #1736 is about: there is NO
    server-side gate on this repo (branch protection is Pro-gated -> HTTP 403), so
    a silent GO on an undeterminable CI state is a merge with no gate at all.
    An UNDETERMINED CI state is not evidence of green."""
    _stub_green_path(monkeypatch, green_raises=True)
    assert pg.main(["--pr", "5", "--repo", "o/r"]) == 3


def test_main_no_green_check_flag_skips_gate(monkeypatch):
    """--no-green-check bypasses the gate entirely (green_gate must not be called)."""
    def must_not_call(*a, **k):
        raise AssertionError("green_gate called despite --no-green-check")
    monkeypatch.setattr(pg, "resolve", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "behind_by": 0,
                         "pr": 5, "base": "main", "state": "OPEN"})
    monkeypatch.setattr(pg, "merge_time_duplicate", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0})
    monkeypatch.setattr(pg, "file_overlap_advisory", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "overlaps": []})
    monkeypatch.setattr(pg, "green_gate", must_not_call)
    assert pg.main(["--pr", "5", "--repo", "o/r", "--no-green-check"]) == 0


# ---- #1736: no server-side gate exists; harden the client-side one ------------
# main went red on 2026-07-20 because PR #1730 merged 7 minutes AFTER its own CI
# run concluded `failure`. Root cause is structural, not a bypassed gate: branch
# protection CANNOT be enabled on this repo (private + free plan ->
# GET /repos/octo/home-repo/branches/main/protection => HTTP 403
# "Upgrade to GitHub Pro"). `test` is therefore not, and cannot be, a REQUIRED
# status check. Nothing on GitHub's side ever blocks a merge over red CI, so this
# gate is the ONLY gate -- which raises the bar on three things it did not do:
#   (1) name the failing check + its run URL so the operator can act in one step,
#   (2) fail CLOSED when the CI state cannot be determined,
#   (3) offer a LOUD, reasoned override instead of a silent env-var bypass.
#
# INSTRUMENT NOTE (the trap that makes a naive guard worse than none): on this
# repo `gh run list --commit <sha>` returns [] even for known-GREEN commits
# (verified against f71e0a73). A guard reading that would see a false empty and
# either fail open or block everything. The instrument used here is
# `gh pr view <n> --json statusCheckRollup`, proven to register BOTH states
# against live data -- the two fixtures below are its verbatim output.

# Verbatim `gh pr view 1730 --json statusCheckRollup` (head 3222d4b6, MERGED red).
_ROLLUP_1730_RED = [
    {"__typename": "CheckRun", "workflowName": "agent-os tests", "name": "test",
     "status": "COMPLETED", "conclusion": "FAILURE", "context": None, "state": None,
     "detailsUrl": "https://github.com/octo/home-repo/actions/runs/"
                   "29748638073/job/88372985192"},
    {"__typename": "CheckRun", "workflowName": "agent-os tests",
     "name": "advance-deploy-ref", "status": "COMPLETED", "conclusion": "SKIPPED",
     "context": None, "state": None,
     "detailsUrl": "https://github.com/octo/home-repo/actions/runs/"
                   "29748638073/job/88374503490"},
]
# Verbatim `gh pr view 1735 --json statusCheckRollup` (head 86e42f86, merged green).
_ROLLUP_1735_GREEN = [
    {"__typename": "CheckRun", "workflowName": "agent-os tests", "name": "test",
     "status": "COMPLETED", "conclusion": "SUCCESS", "context": None, "state": None,
     "detailsUrl": "https://github.com/octo/home-repo/actions/runs/"
                   "29764689569/job/88427851631"},
    {"__typename": "CheckRun", "workflowName": "agent-os tests",
     "name": "advance-deploy-ref", "status": "COMPLETED", "conclusion": "SKIPPED",
     "context": None, "state": None,
     "detailsUrl": "https://github.com/octo/home-repo/actions/runs/"
                   "29764689569/job/88429159244"},
]


# ---- instrument bidirectionality: the same reader must register BOTH states ----
def test_instrument_registers_both_states():
    """Control + treatment on ONE instrument. A guard proven only against green is
    an uninstrumented guard: the null reading (`no failure seen`) would be a fact
    about the reader, not about CI. These two fixtures are live `gh pr view
    --json statusCheckRollup` output for a known-GREEN and a known-RED PR, and the
    reader must return DIFFERENT answers for them."""
    assert pg.evaluate_checks(_ROLLUP_1735_GREEN, "agent-os tests") == "success"
    assert pg.evaluate_checks(_ROLLUP_1730_RED, "agent-os tests") == "failure"


# ---- (1) name the failing check + run URL -------------------------------------
def test_failing_checks_reports_name_and_url():
    """The block message must let the operator act in ONE step: which check failed
    and where to look. A bare 'CI is not green' costs a manual hunt."""
    failing = pg.failing_checks(_ROLLUP_1730_RED, "agent-os tests")
    assert len(failing) == 1, "the SKIPPED sibling is not a failure"
    assert failing[0]["name"] == "test"
    assert failing[0]["outcome"] == "failure"
    assert "29748638073" in failing[0]["url"]


def test_failing_checks_empty_when_green():
    assert pg.failing_checks(_ROLLUP_1735_GREEN, "agent-os tests") == []


def test_failing_checks_includes_pending():
    """A still-running check blocks too, and the operator needs its URL to watch it."""
    failing = pg.failing_checks(_ROLLUP_PENDING, "agent-os tests")
    assert [f["outcome"] for f in failing] == ["pending"]


def test_failing_checks_prefers_targeturl_for_statuscontext():
    """Legacy StatusContext entries carry targetUrl, not detailsUrl."""
    failing = pg.failing_checks(
        [{"__typename": "StatusContext", "context": "agent-os tests",
          "state": "FAILURE", "targetUrl": "https://ci.example/build/7"}],
        "agent-os tests")
    assert failing[0]["url"] == "https://ci.example/build/7"


# ---- (2) fail CLOSED when the CI state cannot be determined --------------------
def test_green_gate_fails_closed_when_probe_never_succeeds(monkeypatch):
    """gh unreachable for the whole budget => HOLD, never GO. 'Cannot determine'
    is not 'green' -- and with no server-side gate there is no backstop."""
    def boom(*a, **k):
        raise RuntimeError("gh: API rate limit exceeded")
    monkeypatch.setattr(pg, "fetch_check_rollup", boom)
    # #2583: "unreadable" now means BOTH transports. Stubbing only the GraphQL
    # reader would let the REST fallback make a real network call from a unit
    # test -- and would no longer be testing what this asserts.
    monkeypatch.setattr(pg, "fetch_rollup_via_rest", boom)
    fc = _FakeClock(step=1.0)
    res = pg.green_gate(1730, repo="o/r", gh="gh", wait_green=5,
                        sleep=fc.sleep, now=fc.now, required=CI)
    assert res["verdict"] == "NOT_GREEN"
    assert res["exit_code"] == pg.GREEN_HOLD == 3
    assert res["ci"] == "undetermined"
    assert "rate limit" in res.get("error", "")


def test_green_gate_retries_through_a_transient_probe_failure(monkeypatch):
    """A single blip must not HOLD a genuinely-green PR: keep polling to the
    deadline. Fail-closed is about the DEADLINE, not about the first hiccup --
    otherwise eng-flow's 1200s wait would wedge on any momentary gh error."""
    seq = [RuntimeError("transient"), RuntimeError("transient"), _ROLLUP_1735_GREEN]
    calls = {"i": 0}
    def fake_fetch(*a, **k):
        r = seq[min(calls["i"], len(seq) - 1)]
        calls["i"] += 1
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(pg, "fetch_check_rollup", fake_fetch)
    # #2583: keep the blip on BOTH transports, so this still exercises the
    # retry-to-deadline path rather than silently falling back to REST.
    monkeypatch.setattr(pg, "fetch_rollup_via_rest",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("transient")))
    fc = _FakeClock(step=1.0)
    res = pg.green_gate(1735, repo="o/r", gh="gh", wait_green=1000,
                        sleep=fc.sleep, now=fc.now, required=CI)
    assert res["verdict"] == "GO"
    assert calls["i"] >= 3


def test_green_gate_undetermined_with_no_wait_holds(monkeypatch):
    """--wait-green 0 (the standalone default) + an unreachable probe => HOLD."""
    def boom(*a, **k):
        raise RuntimeError("gh down")
    monkeypatch.setattr(pg, "fetch_check_rollup", boom)
    monkeypatch.setattr(pg, "fetch_rollup_via_rest", boom)  # #2583: both blind
    res = pg.green_gate(1730, repo="o/r", gh="gh", wait_green=0, required=CI)
    assert res["exit_code"] == 3
    assert res["ci"] == "undetermined"


# ---- #1730 replay: control (green -> GO) then treatment (red -> HOLD) ---------
def test_replay_1735_green_pr_is_allowed(monkeypatch):
    """CONTROL. The guard must not block a legitimately-green merge."""
    monkeypatch.setattr(pg, "fetch_check_rollup", lambda *a, **k: _ROLLUP_1735_GREEN)
    res = pg.green_gate(1735, repo="octo/home-repo", gh="gh", required=CI)
    assert res["verdict"] == "GO"
    assert res["exit_code"] == 0


def test_replay_1730_red_pr_is_blocked(monkeypatch):
    """TREATMENT -- the whole point of #1736. Replayed against PR #1730's ACTUAL
    CI state, the guard must refuse the merge that reddened main, and must name
    the failing check + run 29748638073 so the operator can act in one step."""
    monkeypatch.setattr(pg, "fetch_check_rollup", lambda *a, **k: _ROLLUP_1730_RED)
    res = pg.green_gate(1730, repo="octo/home-repo", gh="gh",
                        wait_green=1200, required=CI)
    assert res["verdict"] == "NOT_GREEN"
    assert res["exit_code"] == pg.GREEN_HOLD == 3
    assert res["ci"] == "failure"
    assert res["failing"][0]["name"] == "test"
    assert "29748638073" in res["failing"][0]["url"]


def test_main_1730_replay_prints_check_name_and_url(monkeypatch, capsys):
    """End-to-end through main(): exit 3 and a message the operator can act on."""
    _stub_green_path(monkeypatch, green_result={
        "verdict": "NOT_GREEN", "exit_code": pg.GREEN_HOLD, "pr": 1730,
        "check": "agent-os tests", "ci": "failure",
        "failing": [{"name": "test", "outcome": "failure",
                     "url": "https://github.com/octo/home-repo/"
                            "actions/runs/29748638073/job/88372985192"}]})
    assert pg.main(["--pr", "1730", "--repo", "octo/home-repo"]) == 3
    out = capsys.readouterr().out
    assert "- test (failure)" in out
    assert "29748638073" in out


# ---- (3) loud, reasoned override ---------------------------------------------
def test_allow_red_without_reason_is_rejected(monkeypatch):
    """--allow-red must carry a real reason. A bare flag is a silent bypass."""
    _stub_green_path(monkeypatch, green_result={
        "verdict": "NOT_GREEN", "exit_code": pg.GREEN_HOLD, "pr": 5,
        "check": "agent-os tests", "ci": "failure", "failing": []})
    assert pg.main(["--pr", "5", "--repo", "o/r", "--allow-red", "   "]) == 3


def test_allow_red_rejects_unedited_placeholder(monkeypatch):
    """Same class as authorize_merge.py's template rejection: pasting the help
    text's <placeholder> must not arm the override."""
    _stub_green_path(monkeypatch, green_result={
        "verdict": "NOT_GREEN", "exit_code": pg.GREEN_HOLD, "pr": 5,
        "check": "agent-os tests", "ci": "failure", "failing": []})
    assert pg.main(["--pr", "5", "--repo", "o/r",
                    "--allow-red", "<why this red merge is safe>"]) == 3


def test_allow_red_rejects_too_short_reason(monkeypatch):
    _stub_green_path(monkeypatch, green_result={
        "verdict": "NOT_GREEN", "exit_code": pg.GREEN_HOLD, "pr": 5,
        "check": "agent-os tests", "ci": "failure", "failing": []})
    assert pg.main(["--pr", "5", "--repo", "o/r", "--allow-red", "ok"]) == 3


def test_allow_red_with_reason_overrides_and_is_loud(monkeypatch, capsys):
    """A real reason releases the hold -- LOUDLY, echoing the reason verbatim so
    it lands in the transcript and the operator cannot bypass by reflex."""
    _stub_green_path(monkeypatch, green_result={
        "verdict": "NOT_GREEN", "exit_code": pg.GREEN_HOLD, "pr": 5,
        "check": "agent-os tests", "ci": "failure", "failing": []})
    reason = "self-hosted runner offline; CI red is infra, not the diff"
    rc = pg.main(["--pr", "5", "--repo", "o/r", "--allow-red", reason])
    assert rc == 0
    captured = capsys.readouterr()
    assert reason in (captured.out + captured.err)
    assert "ALLOW-RED" in (captured.out + captured.err)


def test_allow_red_does_not_override_stale_base(monkeypatch):
    """Scope guard: --allow-red releases the CI-green hold ONLY. A stale base or a
    build-window duplicate is a different failure class and stays held."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k:
                        {"verdict": "STALE", "exit_code": pg.STALE_HOLD,
                         "behind_by": 3, "pr": 5, "base": "main", "state": "OPEN"})
    assert pg.main(["--pr", "5", "--repo", "o/r",
                    "--allow-red", "runner offline, red is infra not the diff"]) == 3


def _stale_res(**over):
    res = {"verdict": "STALE", "exit_code": pg.STALE_HOLD, "behind_by": 1,
           "pr": 2028, "repo": "octo/home-repo", "base": "main",
           "head": "feat/x", "state": "OPEN"}
    res.update(over)
    return res


def test_stale_hold_names_the_one_call_update_branch_command(monkeypatch, capsys):
    """#2024: a HOLD must hand back the exact command that clears it.

    The old message said "re-sync the branch (merge main + push)" -- a LOCAL
    recovery describing a checkout the merger usually does not have. GitHub does
    the whole thing server-side in one call. The stale-base class fires in
    batches by construction (one merge to main puts every other queued PR one
    commit behind), so the un-named command was paid repeatedly, not once."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k: _stale_res())
    assert pg.main(["--pr", "2028", "--repo", "octo/home-repo"]) == 3
    out = capsys.readouterr().out
    assert "update-branch" in out, "STALE hold does not name the one-call fix"
    assert "repos/octo/home-repo/pulls/2028/update-branch" in out, (
        "the emitted command must be copy-pasteable -- owner/repo and PR number "
        "substituted, not a <placeholder> the human has to fill in")


def test_stale_hold_command_never_leaks_a_none_repo(monkeypatch, capsys):
    """`resolve` sets res['repo'] on every STALE path, but a caller-stubbed or
    future-refactored result might not. Printing `repos/None/pulls/...` is worse
    than printing nothing: it looks copy-pasteable and fails on paste."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k: _stale_res(repo=None))
    assert pg.main(["--pr", "2028", "--repo", "octo/home-repo"]) == 3
    out = capsys.readouterr().out
    assert "None" not in out
    assert "repos/octo/home-repo/pulls/2028/update-branch" in out, (
        "should fall back to the --repo argument the caller already passed")


def test_stale_hold_json_output_is_unchanged(monkeypatch, capsys):
    """Scope guard: --json is what merge_go and any other machine consumer would
    parse, so it must not grow or lose keys as a side effect of a human-facing
    message change (its original #2024 purpose).

    #2294 grew it DELIBERATELY by exactly one key: `repo_inferred`. Both
    consumers read the guard by exit code only (`merge_go.py` -> res.returncode,
    `hooks/preflight_auth.py` -> non-zero means do not arm), so an additive key
    breaks nothing -- and naming the inspected repo is the whole point of #2294.
    The equality assertion is kept (not loosened to a subset) so that any FURTHER
    unintended growth still fails here."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k: _stale_res())
    assert pg.main(["--pr", "2028", "--repo", "octo/home-repo",
                    "--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    expected = dict(_stale_res())
    expected["repo_inferred"] = False   # --repo was passed explicitly, not guessed
    assert payload == expected


def test_allow_red_json_records_the_override(monkeypatch, capsys):
    """The override must be machine-visible, not just printed prose."""
    _stub_green_path(monkeypatch, green_result={
        "verdict": "NOT_GREEN", "exit_code": pg.GREEN_HOLD, "pr": 5,
        "check": "agent-os tests", "ci": "failure", "failing": []})
    reason = "runner offline; red is infra, not the diff"
    assert pg.main(["--pr", "5", "--repo", "o/r", "--allow-red", reason,
                    "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "ALLOW_RED"
    assert payload["override_reason"] == reason
    assert payload["overridden"]["ci"] == "failure"


# ---- gh binary resolution (#1912) ------------------------------------------
# The guard used to invoke a bare "gh", which resolves only through the caller's
# PATH. A Claude Code hook subprocess does not inherit the shell profile that
# puts the GitHub CLI there, so every `mgo <N>` preflight died with
# [WinError 2] and returned UNDETERMINED -- fail-closed, but permanently.
#
# Cross-platform by construction: the candidate list is injected and the
# "working binary" is sys.executable, which answers --version with rc 0 on every
# platform. Nothing here depends on a Windows install path (the CI runner is
# Linux -- see feedback-ci-checks-cross-platform).

def test_bare_gh_falls_back_to_a_candidate_that_responds(monkeypatch):
    """A bare "gh" that PATH cannot resolve falls through to a working candidate."""
    monkeypatch.setattr(pg, "_GH_RESOLVED", None, raising=False)
    resolved = pg._resolve_gh(
        "gh", candidates=("definitely-not-a-real-binary-1912", sys.executable))
    assert resolved == sys.executable


def test_resolution_probes_rather_than_trusting_the_name(monkeypatch):
    """A candidate that exists but does not answer --version is not accepted.

    Existence is not responsiveness -- the same distinction _find_gh() makes in
    catch_up.py. A path that resolves but errors would otherwise be handed to
    every downstream gh call and fail one layer deeper.
    """
    monkeypatch.setattr(pg, "_GH_RESOLVED", None, raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[0])
        class R:
            returncode = 1 if cmd[0] == "answers-nothing" else 0
        return R()

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    assert pg._resolve_gh("gh", candidates=("answers-nothing", "real-gh")) == "real-gh"
    assert calls == ["answers-nothing", "real-gh"]


def test_an_explicit_gh_path_is_never_probed(monkeypatch):
    """The --gh injection seam must pass through untouched.

    Only the literal default "gh" is ambiguous; anything the caller named
    explicitly is already a decision and must not be second-guessed.
    """
    monkeypatch.setattr(pg, "_GH_RESOLVED", None, raising=False)

    def explode(*a, **kw):
        raise AssertionError("an explicit gh path must not be probed")

    monkeypatch.setattr(pg.subprocess, "run", explode)
    assert pg._resolve_gh("/opt/fake/gh") == "/opt/fake/gh"


def test_resolution_is_cached_across_calls(monkeypatch):
    """_gh_json runs several times per guard invocation; probe once, not per call."""
    monkeypatch.setattr(pg, "_GH_RESOLVED", None, raising=False)
    probes = []

    def fake_run(cmd, **kw):
        probes.append(cmd[0])
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    first = pg._resolve_gh("gh", candidates=("cached-gh",))
    second = pg._resolve_gh("gh", candidates=("cached-gh",))
    assert first == second == "cached-gh"
    assert probes == ["cached-gh"], "resolution must be memoized, not re-probed"


def test_unresolvable_gh_names_itself_instead_of_raising_winerror(monkeypatch):
    """No candidate responds -> a named error, not a bare OS error.

    UNDETERMINED already means "GitHub did not answer" (see UNDETERMINED_CI).
    "gh is not installed/on PATH" is a different operator action -- fix the
    environment, not the connectivity -- so the message must say which it is.
    """
    monkeypatch.setattr(pg, "_GH_RESOLVED", None, raising=False)

    def always_missing(*a, **kw):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(pg.subprocess, "run", always_missing)
    with pytest.raises(RuntimeError) as exc:
        pg._resolve_gh("gh", candidates=("nope-a", "nope-b"))
    msg = str(exc.value)
    assert "gh" in msg
    assert "nope-a" in msg and "nope-b" in msg, "must name what it tried"
    assert "WinError" not in msg


# ---- slow probe != missing binary (#2692) ----------------------------------
# `subprocess.TimeoutExpired` IS a subclass of `subprocess.SubprocessError`, so
# a single `except (OSError, SubprocessError)` swallowed "gh did not answer in
# time" into the same branch as "gh is not there" -- and the raise then asserted
# the second. The operator was sent to fix a PATH that was fine.
#
# The budget is not the interesting part: measured quiescent on one host, N=12,
# `gh --version` ran min 0.085s / median 0.093s / max 0.108s against a 5s
# ceiling -- ~54x of headroom. It expires only under the load this gate runs in
# (CI waits, parallel lanes, a dashboard sweep), i.e. its normal operating
# condition. Widening the budget alone would not split the two causes.

def test_a_slow_probe_is_reported_as_unresponsive_not_as_missing(monkeypatch):
    """A probe that TIMES OUT must not be reported as an absent install."""
    monkeypatch.setattr(pg, "_GH_RESOLVED", None, raising=False)

    def always_slow(cmd, **kw):
        raise pg.subprocess.TimeoutExpired(cmd, pg.GH_PROBE_TIMEOUT_S)

    monkeypatch.setattr(pg.subprocess, "run", always_slow)
    with pytest.raises(RuntimeError) as exc:
        pg._resolve_gh("gh", candidates=("slow-gh",))
    msg = str(exc.value)
    assert "slow-gh" in msg, "must name the candidate that hung"
    assert "UNRESPONSIVE" in msg or "unresponsive" in msg
    # The whole defect: the operator was told to fix an environment that is fine.
    # These two strings are the ABSENT branch's own words -- asserting their
    # absence only discriminates because they really do appear over there
    # (test_a_genuinely_absent_gh_... pins that they still do).
    assert "environment gap" not in msg
    assert "Install the GitHub CLI" not in msg
    assert "broken PATH" in msg, (
        "the message must point AT retrying and AWAY from a PATH hunt")


def test_the_probe_argv_default_is_still_version():
    """The seam below is only safe while its DEFAULT stays the shipping argv.

    `GH_PROBE_ARGS` exists so a test can drive the timeout branch; nothing else
    should ever change it. Every other test injects an explicit `--gh` path,
    which returns before the probe runs, so a corrupted default would not redden
    any of them -- this is the one assertion standing between that and a probe
    that asks a real `gh` the wrong question.
    """
    assert pg.GH_PROBE_ARGS == ("--version",)


def test_a_real_timeout_takes_the_timeout_branch(monkeypatch):
    """Positive control: the REAL subprocess machinery, not a stubbed raise.

    A stubbed `TimeoutExpired` proves only that the handler exists. This drives a
    real `subprocess.run` past a real deadline, so it proves the branch the
    shipping code actually lands on.

    The deadline is unmissable BY CONSTRUCTION rather than by being faster than
    the runner: the child sleeps orders of magnitude longer than the budget, so
    it CANNOT have exited when the deadline is checked. That is the whole
    difference from the race this replaces (#2710/#2719), which bet a 1ms budget
    against `--version`.

    Elapsed time is not what raises, and spawn cost is not inside the budget:
    `run(timeout=)` spawns the child FIRST and starts the clock after (CPython
    3.12.10 `subprocess.py` -- `with Popen(...)` at run():548, then
    `endtime = _time() + timeout` inside communicate():1204), and the raise then
    hinges on whether the wait is still PENDING when the parent looks -- on
    Windows literally `is_alive()` on the reader thread, _communicate():1629. A
    child that has already finished returns normally however far past the
    deadline the call ran. So no timeout can fire "during spawn", and the old
    bet was only ever that the child would still be running at the check.

    Under load it often was not. Replaying the old mechanism here, the
    assertion lost 0 of 40 at rest but 2-7% across 8- and 24-way concurrent
    spawns: load-correlated, not margin-correlated, which is why it reddened
    `main` on a docs-only commit (8aab442e, taking `advance-deploy-ref` with
    it) and cost round-trips on unrelated PRs, yet never reproduced by hand.
    Measurements in #2728.

    The 0.25s budget is a runtime choice, not a correctness one -- with the
    child sleeping 30s, any shorter budget lands on the same branch. What the
    sleeping child buys is fidelity to the branch's own message, which claims
    the candidate "started and did not answer".

    Cross-platform by construction (see the section header above): no sleeping
    shell script, no exec bit, nothing Windows-specific.
    """
    monkeypatch.setattr(pg, "_GH_RESOLVED", None, raising=False)
    monkeypatch.setattr(pg, "GH_PROBE_ARGS", ("-c", "import time; time.sleep(30)"))
    monkeypatch.setattr(pg, "GH_PROBE_TIMEOUT_S", 0.25)
    with pytest.raises(RuntimeError) as exc:
        pg._resolve_gh("gh", candidates=(sys.executable,))
    msg = str(exc.value)
    assert "unresponsive" in msg.lower(), (
        "a real timeout must reach the timeout branch, not the absent branch; "
        "got: %s" % msg)
    assert "environment gap" not in msg


def test_a_genuinely_absent_gh_still_reports_the_environment_gap(monkeypatch):
    """The other direction: splitting the branch must not blur the absent case.

    Pins the wording both ways so a future collapse is caught whichever way it
    collapses -- `test_unresolvable_gh_names_itself...` above asserts the error
    names its candidates, this asserts it still names the CAUSE.
    """
    monkeypatch.setattr(pg, "_GH_RESOLVED", None, raising=False)

    def always_missing(*a, **kw):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(pg.subprocess, "run", always_missing)
    with pytest.raises(RuntimeError) as exc:
        pg._resolve_gh("gh", candidates=("nope",))
    msg = str(exc.value)
    assert "Install the GitHub CLI" in msg and "environment gap" in msg
    assert "unresponsive" not in msg.lower(), (
        "an absent binary must not borrow the timeout wording")


def test_a_slow_candidate_does_not_block_a_working_one(monkeypatch):
    """The timeout branch CONTINUES; it must not short-circuit resolution.

    A hung first candidate with a healthy second still resolves -- reporting
    "unresponsive" when a usable binary was found one line later would trade one
    false verdict for another.
    """
    monkeypatch.setattr(pg, "_GH_RESOLVED", None, raising=False)
    tried = []

    def slow_then_fine(cmd, **kw):
        tried.append(cmd[0])
        if cmd[0] == "slow-gh":
            raise pg.subprocess.TimeoutExpired(cmd, pg.GH_PROBE_TIMEOUT_S)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(pg.subprocess, "run", slow_then_fine)
    assert pg._resolve_gh("gh", candidates=("slow-gh", "real-gh")) == "real-gh"
    assert tried == ["slow-gh", "real-gh"]


def test_gh_json_routes_through_resolution(monkeypatch):
    """The fix must sit on the path _gh_json actually takes, not beside it."""
    monkeypatch.setattr(pg, "_GH_RESOLVED", None, raising=False)
    seen = {}

    def fake_run(cmd, **kw):
        seen["argv0"] = cmd[0]
        class R:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""
        return R()

    monkeypatch.setattr(pg, "_resolve_gh", lambda gh, **kw: "/resolved/gh")
    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    assert pg._gh_json("gh", ["pr", "view", "1"]) == {"ok": True}
    assert seen["argv0"] == "/resolved/gh"


# ---- resolve_required_check: per-repo CI check derivation (#1914) -----------
#
# premerge_guard.py:148 hardcoded REQUIRED_CHECK_DEFAULT = "agent-os tests" --
# home-repo's own workflow name -- as the check every invocation gates on,
# even against a --repo whose CI has a different workflow name entirely (e.g.
# OtherRepo's is "ci"). No caller in the merge chain passes --required-check, so
# every cross-repo merge HELD FOREVER on a check the target repo cannot produce.

def test_resolve_required_check_explicit_override_wins_without_a_gh_call():
    """An explicit --required-check always wins verbatim -- and must not even
    trigger a workflow-list probe (the operator already decided)."""
    def boom(gh, args):
        raise AssertionError("must not call gh when required_check is explicit")
    assert pg.resolve_required_check("octo/other-repo", gh="gh",
                                     required_check="custom-check") == "custom-check"
    # (boom is unused by design -- resolve_required_check must never reach _gh_json
    # in this branch; asserting via a real monkeypatch is done in the next test.)


def test_resolve_required_check_no_repo_is_unresolved(monkeypatch):
    """No repo and no option -> the strict UNRESOLVED sentinel, no gh call."""
    def boom(gh, args):
        raise AssertionError("must not call gh when repo is None")
    monkeypatch.setattr(pg, "_gh_json", boom)
    assert pg.resolve_required_check(None, gh="gh") == pg.REQUIRED_CHECK_DEFAULT


def test_resolve_required_check_option_wins_over_the_derive(monkeypatch):
    """The plugin's `required_check` option (CLAUDE_PLUGIN_OPTION_REQUIRED_CHECK,
    exported to hook processes) beats the workflow derive, with no gh call."""
    def boom(gh, args):
        raise AssertionError("must not call gh when the option is set")
    monkeypatch.setattr(pg, "_gh_json", boom)
    monkeypatch.setenv(pg.REQUIRED_CHECK_ENV, "build")
    assert pg.resolve_required_check("octo/many", gh="gh") == "build"
    assert pg.resolve_required_check(None, gh="gh") == "build"


def test_resolve_required_check_explicit_flag_beats_the_option(monkeypatch):
    monkeypatch.setenv(pg.REQUIRED_CHECK_ENV, "build")
    assert pg.resolve_required_check("octo/many", gh="gh",
                                     required_check="custom-check") == "custom-check"


def test_resolve_required_check_blank_option_is_unset(monkeypatch):
    """An option saved empty is exported empty: it must not pin a blank name."""
    monkeypatch.setenv(pg.REQUIRED_CHECK_ENV, "   ")
    monkeypatch.setattr(pg, "_gh_json", lambda gh, args: {"workflows": [{"name": "ci"}]})
    assert pg.resolve_required_check("octo/one", gh="gh") == "ci"


def test_resolve_required_check_two_workflows_without_option_is_unresolved(monkeypatch):
    """No workflow name is special in the kit. Two workflows and no option is
    ambiguous -> the strict sentinel. Neither index may be picked: a "take
    names[0]" or "take names[-1]" mutation fails here."""
    def fake_gh_json(gh, args):
        assert args == ["api", "repos/octo/home-repo/actions/workflows"]
        return {"workflows": [{"name": "visual-verify gate"},
                               {"name": "agent-os tests"}]}
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    assert pg.resolve_required_check("octo/home-repo", gh="gh") == \
        pg.REQUIRED_CHECK_DEFAULT


def test_unresolved_sentinel_matches_no_real_check_and_names_the_fix():
    """The strict fallback HOLDs because no real check-run can carry the
    sentinel's name, and its text is the remedy the operator reads."""
    rollup = [{"__typename": "CheckRun", "workflowName": name, "name": name,
               "status": "COMPLETED", "conclusion": "SUCCESS"}
              for name in ("ci", "test", "build", "agent-os tests", "")]
    assert pg.evaluate_checks(rollup, pg.REQUIRED_CHECK_DEFAULT) == "absent"
    assert "--required-check" in pg.REQUIRED_CHECK_DEFAULT
    assert "required_check" in pg.REQUIRED_CHECK_DEFAULT.replace("--required-check", "")


def test_resolve_required_check_single_foreign_workflow_is_derived(monkeypatch):
    """AC1: OtherRepo has exactly one workflow, named 'ci' -- not 'agent-os
    tests'. The guard must derive and gate on 'ci', not the home-repo default."""
    def fake_gh_json(gh, args):
        assert args == ["api", "repos/octo/other-repo/actions/workflows"]
        return {"workflows": [{"name": "ci"}]}
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    assert pg.resolve_required_check("octo/other-repo", gh="gh") == "ci"


def test_resolve_required_check_ambiguous_multi_workflow_falls_back_strict(monkeypatch):
    """>1 workflow, none matching the constant -- no safe single guess, so fall
    back to the STRICT constant (never a permissive wrong guess)."""
    def fake_gh_json(gh, args):
        return {"workflows": [{"name": "lint"}, {"name": "deploy"}]}
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    assert pg.resolve_required_check("o/many", gh="gh") == pg.REQUIRED_CHECK_DEFAULT


def test_resolve_required_check_no_workflows_falls_back_strict(monkeypatch):
    def fake_gh_json(gh, args):
        return {"workflows": []}
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    assert pg.resolve_required_check("o/empty", gh="gh") == pg.REQUIRED_CHECK_DEFAULT


def test_resolve_required_check_probe_failure_falls_back_strict(monkeypatch):
    """A workflow-list probe failure must fall back to the STRICT constant, not
    silently accept anything -- an unresolvable repo still gates on 'agent-os
    tests', which its rollup will never contain, so it HOLDs rather than waves a
    merge through on a guess."""
    def fake_gh_json(gh, args):
        raise RuntimeError("gh api failed: 404")
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    assert pg.resolve_required_check("o/gone", gh="gh") == pg.REQUIRED_CHECK_DEFAULT


# ---- main() end-to-end: derivation actually wired into the merge gate (#1914) -

def _stub_dag_go(monkeypatch, pr):
    """Stub the base-freshness / dup / overlap gates to GO, leaving the verdict
    to whatever the (real, un-stubbed) green_gate + resolve_required_check derive
    from the fake `_gh_json` dispatcher each test below installs."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "behind_by": 0,
                         "pr": pr, "base": "main", "state": "OPEN"})
    monkeypatch.setattr(pg, "merge_time_duplicate", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0})
    monkeypatch.setattr(pg, "file_overlap_advisory", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "overlaps": []})


def test_main_cross_repo_derives_check_and_goes_green(monkeypatch):
    """AC1 (#1914) end-to-end: a green OtherRepo PR must not HOLD on
    home-repo's own check name -- main() must derive 'ci' and gate on it."""
    _stub_dag_go(monkeypatch, 483)

    def fake_gh_json(gh, args):
        if args == ["api", "repos/octo/other-repo/actions/workflows"]:
            return {"workflows": [{"name": "ci"}]}
        if args[:2] == ["pr", "view"]:
            return {"statusCheckRollup": [
                {"__typename": "CheckRun", "workflowName": "ci", "name": "test",
                 "status": "COMPLETED", "conclusion": "SUCCESS"}]}
        raise AssertionError("unexpected gh call: %r" % (args,))

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    assert pg.main(["--pr", "483", "--repo", "octo/other-repo", "--json"]) == 0


def _two_workflow_gh_json(gh, args):
    """A repo with two workflows whose PR is fully green on both."""
    if args == ["api", "repos/octo/home-repo/actions/workflows"]:
        return {"workflows": [{"name": "lint"}, {"name": "build"}]}
    if args[:2] == ["pr", "view"]:
        return {"statusCheckRollup": [
            {"__typename": "CheckRun", "workflowName": "lint", "name": "lint",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "workflowName": "build", "name": "test",
             "status": "COMPLETED", "conclusion": "SUCCESS"}]}
    raise AssertionError("unexpected gh call: %r" % (args,))


def test_main_two_workflow_repo_without_option_holds(monkeypatch, capsys):
    """G4: with no --required-check and no option, a two-workflow repo has no
    check to gate on. The strict fallback HOLDs even though everything that ran
    is green, and the hold names the sentinel (the remedy)."""
    _stub_dag_go(monkeypatch, 900)
    monkeypatch.setattr(pg, "_gh_json", _two_workflow_gh_json)
    assert pg.main(["--pr", "900", "--repo", "octo/home-repo", "--json"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["check"] == pg.REQUIRED_CHECK_DEFAULT
    assert out["ci"] == "absent"


def test_main_two_workflow_repo_with_option_resolves(monkeypatch):
    """G4: the same repo with the plugin option set gates on that check -> GO."""
    _stub_dag_go(monkeypatch, 900)
    monkeypatch.setattr(pg, "_gh_json", _two_workflow_gh_json)
    monkeypatch.setenv(pg.REQUIRED_CHECK_ENV, "build")
    assert pg.main(["--pr", "900", "--repo", "octo/home-repo", "--json"]) == 0


def test_main_two_workflow_repo_with_flag_resolves(monkeypatch):
    """From the Bash tool the option is not in the environment (plugin docs:
    only hook processes get it), so --required-check is that path's way in."""
    _stub_dag_go(monkeypatch, 900)
    monkeypatch.setattr(pg, "_gh_json", _two_workflow_gh_json)
    assert pg.main(["--pr", "900", "--repo", "octo/home-repo",
                    "--required-check", "build", "--json"]) == 0


def test_main_derived_check_absent_from_rollup_still_holds(monkeypatch):
    """AC3 (#1914): deriving a real per-repo check name must not turn 'CI never
    ran' into a false GO -- an empty rollup is still absent -> HOLD."""
    _stub_dag_go(monkeypatch, 500)

    def fake_gh_json(gh, args):
        if args == ["api", "repos/octo/other-repo/actions/workflows"]:
            return {"workflows": [{"name": "ci"}]}
        if args[:2] == ["pr", "view"]:
            return {"statusCheckRollup": []}  # nothing has run yet
        raise AssertionError("unexpected gh call: %r" % (args,))

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    assert pg.main(["--pr", "500", "--repo", "octo/other-repo",
                    "--json"]) == 3


# ---- #2743: the derive must see the INFERRED repo, not args.repo ------------
# hooks/preflight_auth.py runs the guard with cwd=<session cwd> and NO --repo
# (#2294), so `gh` infers the session's repo while args.repo stays None. #1914's
# resolver was still handed args.repo, and `if not repo:` returned the home
# constant -- every `/Mgo <N>` from an other-repo cwd gated on 'agent-os tests',
# read `ci: absent`, and HOLD a green PR (2026-09-11, other-repo #884).


def _stub_dag_go_inferred(monkeypatch, pr, repo):
    """Like _stub_dag_go, but the base resolution reports an INFERRED repo --
    the shape `resolve()` returns when the hook invokes the guard bare."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "behind_by": 0,
                         "pr": pr, "base": "main", "state": "OPEN",
                         "repo": repo, "repo_inferred": True})
    monkeypatch.setattr(pg, "merge_time_duplicate", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0})
    monkeypatch.setattr(pg, "file_overlap_advisory", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "overlaps": []})


def _other_repo_gh(rollup_conclusion):
    """A gh dispatcher for the inferred-other-repo case: one workflow named
    `ci`, and a rollup carrying only that workflow at `rollup_conclusion`."""
    def fake_gh_json(gh, args):
        if args == ["api", "repos/octo/other-repo/actions/workflows"]:
            return {"workflows": [{"name": "ci"}]}
        if args[:2] == ["pr", "view"]:
            return {"statusCheckRollup": [
                {"__typename": "CheckRun", "workflowName": "ci", "name": "gate",
                 "status": "COMPLETED", "conclusion": rollup_conclusion}]}
        raise AssertionError("unexpected gh call: %r" % (args,))
    return fake_gh_json


def test_main_inferred_foreign_repo_derives_check_and_goes_green(monkeypatch, capsys):
    """#2743 AC1: bare `--pr N --json` from a cwd whose repo `gh` infers as
    other-repo must derive `ci` from THAT repo and GO on its green rollup --
    not demand 'agent-os tests' and HOLD on `ci: absent`."""
    _stub_dag_go_inferred(monkeypatch, 884, "octo/other-repo")
    monkeypatch.setattr(pg, "_gh_json", _other_repo_gh("SUCCESS"))
    assert pg.main(["--pr", "884", "--json"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["repo"] == "octo/other-repo"
    assert out["repo_inferred"] is True


def test_main_inferred_foreign_repo_still_holds_on_red(monkeypatch, capsys):
    """#2743 AC2 (discrimination): the same inferred derive must HOLD when the
    derived check is red -- the fix routes the repo, it does not wave merges."""
    _stub_dag_go_inferred(monkeypatch, 884, "octo/other-repo")
    monkeypatch.setattr(pg, "_gh_json", _other_repo_gh("FAILURE"))
    assert pg.main(["--pr", "884", "--json"]) == 3
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["verdict"] == "NOT_GREEN"
    assert out["check"] == "ci"
    assert out["ci"] == "failure"


def test_main_resync_path_derives_check_from_inferred_repo(monkeypatch):
    """#2743 AC3: the --resync waiter is the second call site -- it must be
    handed the check derived from the inferred repo, not the home constant."""
    seen = {"required": None, "resolves": 0}

    def fake_resolve(*a, **k):
        seen["resolves"] += 1
        verdict = "STALE" if seen["resolves"] == 1 else "GO"
        return {"verdict": verdict, "exit_code": 3 if verdict == "STALE" else 0,
                "behind_by": 1 if verdict == "STALE" else 0, "pr": 884,
                "base": "main", "state": "OPEN",
                "repo": "octo/other-repo", "repo_inferred": True}

    def fake_resync(*a, **k):
        seen["required"] = k.get("required")
        return {"verdict": "GO", "exit_code": 0, "pr": 884, "ci": "success"}

    def fake_gh_json(gh, args):
        if args == ["api", "repos/octo/other-repo/actions/workflows"]:
            return {"workflows": [{"name": "ci"}]}
        raise AssertionError("unexpected gh call: %r" % (args,))

    monkeypatch.setattr(pg, "resolve", fake_resolve)
    monkeypatch.setattr(pg, "resync_and_wait", fake_resync)
    monkeypatch.setattr(pg, "merge_time_duplicate", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0})
    monkeypatch.setattr(pg, "file_overlap_advisory", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "overlaps": []})
    monkeypatch.setattr(pg, "green_gate", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "ci": "success"})
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    assert pg.main(["--pr", "884", "--json", "--resync"]) == 0
    assert seen["required"] == "ci"


# ---- #2117: automated STALE-HOLD clearing (resync + wait on the NEW base) -----
# The guard's STALE hold is right and unchanged; what was missing is that clearing
# it was entirely manual. One merge to main puts every other queued PR one commit
# behind, so N queued PRs cost N-1 hand-issued `update-branch` calls plus N-1 CI
# re-runs. Observed 2026-08-14/15: four STALE-HOLD fires on the same two PRs
# (#2109 + #2112) in one session.
#
# The load-bearing property under test is NOT "the API call got issued" -- it is
# that readiness is judged against the POST-RESYNC head SHA, and that a base move
# DURING the wait invalidates readiness instead of being ignored. Fire 4 of that
# session is exactly this: `premerge_guard --pr 2109` returned GO, and #2115
# merged ~16 minutes later, staling it again before the merge. A GO is a snapshot
# with no lease.


class _FakeGh:
    """A movable GitHub for the resync path: a PR head SHA, a behind-count, and a
    check rollup, all mutable MID-POLL via ``on_poll``.

    That mutability is the whole point -- a fixed fixture cannot express "a
    concurrent session merged to main while we were waiting", which is the failure
    the resync waiter exists to catch.

    ``on_poll(fake, i)`` fires after the COMPARE call that ends probe ``i`` --
    i.e. a concurrent event lands strictly BETWEEN two probes, never inside one.
    Mutating mid-probe would hand the code under test a self-inconsistent reading
    (a head SHA from before the event, a behind-count from after) that no real
    sequence can produce, and would silently defeat the probe's own binding."""

    def __init__(self, pr=2109, head_sha="head0", behind_by=1, rollup=None,
                 on_poll=None, repo="octo/home-repo"):
        self.pr, self.head_sha, self.behind_by = pr, head_sha, behind_by
        self.rollup = rollup if rollup is not None else _ROLLUP_GREEN
        self.on_poll, self.repo = on_poll, repo
        self.polls = 0
        self.update_branch_calls = []
        self.compare_refs = []

    def __call__(self, gh, args):
        if args[:2] == ["pr", "view"]:
            return {"number": self.pr, "state": "OPEN", "headRefName": "feat/x",
                    "headRefOid": self.head_sha, "baseRefName": "main",
                    "statusCheckRollup": self.rollup}
        if args[:2] == ["api", "--method"]:
            self.update_branch_calls.append(args)
            return {"message": "Updating pull request branch."}
        if args[0] == "api" and "/check-runs" in args[1]:
            # A CONSISTENT GitHub: the runs attached to this head are exactly the
            # ones its rollup reports. The inconsistent window that #2232 fires in
            # is modelled by _LaggingRollupGh, which overrides this.
            return {"check_runs": [{"details_url": c.get("detailsUrl")}
                                   for c in self.rollup if c.get("detailsUrl")]}
        if args[0] == "api" and "/compare/" in args[1]:
            self.compare_refs.append(args[1])
            answer = {"status": "behind" if self.behind_by else "ahead",
                      "behind_by": self.behind_by}
            if self.on_poll:                  # the concurrent event, between probes
                self.on_poll(self, self.polls)
            self.polls += 1
            return answer
        raise AssertionError("unexpected gh call: %r" % (args,))


# ---- the bound probe: rollup, head SHA and behind-count from ONE reading ------

def test_probe_compares_against_the_head_sha_not_the_ref(monkeypatch):
    """DoD (2), instrument half: the behind-count must be computed for the exact
    SHA whose rollup we just read.

    Comparing ``main...<branch-name>`` re-resolves the ref on GitHub's side, so a
    push landing between the two calls yields a behind-count describing a
    DIFFERENT commit than the green we are about to trust. Pinning the SHA makes
    the three readings (SHA, its checks, its behind-count) one bound object."""
    fake = _FakeGh(head_sha="deadbeef", behind_by=0)
    monkeypatch.setattr(pg, "_gh_json", fake)
    probe = pg.probe_merge_readiness(2109, repo="o/r", gh="gh",
                                     required="agent-os tests")
    assert probe["head_sha"] == "deadbeef"
    assert probe["behind_by"] == 0
    assert probe["ci"] == "success"
    assert fake.compare_refs == ["repos/o/r/compare/main...deadbeef"], \
        "the compare must pin the head SHA, not the branch name"


# ---- the pure verdict over one bound probe -----------------------------------

def test_evaluate_resync_waits_until_the_resync_lands():
    """Head SHA unchanged => `update-branch` has not landed yet. Not a verdict:
    the branch is still the stale object, and its checks still describe the stale
    base. WAITING, never GO."""
    probe = {"head_sha": "head0", "behind_by": 1, "ci": "success", "failing": []}
    assert pg.evaluate_resync(probe, "head0")["verdict"] == "WAITING"


def test_evaluate_resync_go_when_fresh_and_green():
    probe = {"head_sha": "head1", "behind_by": 0, "ci": "success", "failing": []}
    res = pg.evaluate_resync(probe, "head0")
    assert res["verdict"] == "GO"
    assert res["exit_code"] == pg.GO == 0


def test_evaluate_resync_base_move_during_wait_holds():
    """DoD (3) -- THE test. A concurrent merge to main during the wait leaves the
    resynced branch behind again. Its CI is green, and that green is exactly as
    untrustworthy as the green the STALE hold exists to refuse: it ran against a
    base that has since moved.

    Green + behind MUST be HOLD. Any ordering that lets 'ci == success' answer
    first re-creates fire 4 of the 2026-08-14/15 session (a verified GO, staled 16
    minutes later, merged anyway)."""
    probe = {"head_sha": "head1", "behind_by": 1, "ci": "success", "failing": []}
    res = pg.evaluate_resync(probe, "head0")
    assert res["verdict"] == "BASE_MOVED"
    assert res["exit_code"] == pg.STALE_HOLD == 3
    assert res["behind_by"] == 1


def test_evaluate_resync_red_ci_on_the_new_base_holds():
    """The resync did its job and the truth it exposed is that the PR is red on
    the fresh base -- the #841->#842 class caught, not cleared."""
    probe = {"head_sha": "head1", "behind_by": 0, "ci": "failure",
             "failing": [{"name": "test", "outcome": "failure", "url": "u"}]}
    res = pg.evaluate_resync(probe, "head0")
    assert res["verdict"] == "NOT_GREEN"
    assert res["exit_code"] == pg.GREEN_HOLD == 3


def test_evaluate_resync_pending_on_the_new_head_waits():
    """CI re-running on the new head is the expected mid-state, not a hold."""
    probe = {"head_sha": "head1", "behind_by": 0, "ci": "pending", "failing": []}
    assert pg.evaluate_resync(probe, "head0")["verdict"] == "WAITING"


def test_evaluate_resync_absent_checks_wait_then_never_go():
    """'absent' is the first reading after a resync (the new run has not been
    reported yet) -- it must WAIT, and only the deadline turns it into a hold."""
    probe = {"head_sha": "head1", "behind_by": 0, "ci": "absent", "failing": []}
    assert pg.evaluate_resync(probe, "head0")["verdict"] == "WAITING"


# ---- the driver: request the update, then wait on the NEW head ---------------

def test_resync_and_wait_clears_a_stale_hold(monkeypatch):
    """DoD (1) end-to-end: a STALE hold clears with no hand-issued API call.

    Models the real sequence: PUT update-branch -> head moves -> CI re-runs
    (pending) on the new head -> green -> GO."""
    def on_poll(fake, i):
        if i == 0:
            fake.head_sha, fake.behind_by = "head1", 0   # resync landed
            fake.rollup = _ROLLUP_PENDING                # CI re-running
        if i == 2:
            fake.rollup = _ROLLUP_GREEN                  # green on the new base

    fake = _FakeGh(head_sha="head0", behind_by=1, on_poll=on_poll)
    monkeypatch.setattr(pg, "_gh_json", fake)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(2109, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=1000, sleep=fc.sleep, now=fc.now)
    assert res["verdict"] == "GO"
    assert res["exit_code"] == 0
    assert len(fake.update_branch_calls) == 1, "exactly one update-branch request"
    assert fake.update_branch_calls[0] == [
        "api", "--method", "PUT", "repos/o/r/pulls/2109/update-branch"]


def test_resync_and_wait_holds_when_the_base_moves_mid_wait(monkeypatch):
    """DoD (3) at driver level: the resync lands and CI goes green, then a
    concurrent session merges to main. The waiter must exit HOLD, not GO -- the
    green it is holding is once again a green against a moved base."""
    def on_poll(fake, i):
        if i == 0:
            fake.head_sha, fake.behind_by = "head1", 0   # resync landed, fresh
            fake.rollup = _ROLLUP_PENDING
        if i == 2:
            fake.rollup = _ROLLUP_GREEN                  # green...
            fake.behind_by = 1                           # ...and main just moved

    fake = _FakeGh(head_sha="head0", behind_by=1, on_poll=on_poll)
    monkeypatch.setattr(pg, "_gh_json", fake)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(2109, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=1000, sleep=fc.sleep, now=fc.now)
    assert res["verdict"] == "BASE_MOVED", "a green against a moved base is a HOLD"
    assert res["exit_code"] == 3


def test_resync_and_wait_holds_when_the_resync_never_lands(monkeypatch):
    """update-branch accepted (202) but the head never moves -> the deadline must
    end the wait with a HOLD. An unbounded waiter would wedge the merge path."""
    fake = _FakeGh(head_sha="head0", behind_by=1, rollup=_ROLLUP_GREEN)
    monkeypatch.setattr(pg, "_gh_json", fake)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(2109, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=5, poll_interval=1, sleep=fc.sleep, now=fc.now)
    assert res["exit_code"] == 3
    assert res["verdict"] == "TIMEOUT"


def test_resync_and_wait_holds_when_the_update_branch_call_fails(monkeypatch):
    """A refused update-branch (409 merge conflict is the common one) leaves the
    hold standing, named. Fail CLOSED: we did not clear anything."""
    def fake_gh_json(gh, args):
        if args[:2] == ["pr", "view"]:
            return {"number": 1, "state": "OPEN", "headRefName": "f",
                    "headRefOid": "head0", "baseRefName": "main",
                    "statusCheckRollup": _ROLLUP_GREEN}
        if args[:2] == ["api", "--method"]:
            raise RuntimeError("gh api failed: HTTP 409 merge conflict")
        return {"behind_by": 1}

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(1, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=10, sleep=fc.sleep, now=fc.now)
    assert res["verdict"] == "RESYNC_FAILED"
    assert res["exit_code"] == 3
    assert "409" in res.get("error", "")


def test_resync_and_wait_probe_failure_retries_then_holds(monkeypatch):
    """A probe blip must not decide anything: retry for the rest of the budget,
    and hold only if no answer ever arrives (same fail-CLOSED contract as
    green_gate, for the same reason -- there is no server-side gate behind us)."""
    calls = {"n": 0}

    def fake_gh_json(gh, args):
        if args[:2] == ["api", "--method"]:
            return {"message": "ok"}
        if args[:2] == ["pr", "view"] and calls["n"] == 0:
            calls["n"] += 1
            return {"number": 1, "state": "OPEN", "headRefName": "f",
                    "headRefOid": "head0", "baseRefName": "main",
                    "statusCheckRollup": []}
        raise RuntimeError("gh down")

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(1, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=5, poll_interval=1, sleep=fc.sleep, now=fc.now)
    assert res["exit_code"] == 3
    assert res.get("error"), "the last probe error must be carried, not swallowed"


# ---- #2817: the readiness probe's REST leg + an honest RESYNC_FAILED -------------
#
# 2026-09-23, PR #2814: under a GraphQL secondary limit `--resync` died on its
# FIRST probe (`gh pr view ... statusCheckRollup`, no REST leg) and printed
# "GitHub refused the update-branch request" + a 409 hint -- though no request was
# ever sent. A hand-issued REST update-branch seconds later succeeded.

_SHA_A = "a" * 40
_SHA_B = "b" * 40


class _GraphqlDeadGh:
    """GraphQL (`pr view`, `repo view`) raises; REST answers. ``resynced`` flips
    on the update-branch PUT: the head moves to _SHA_B and the PR is current."""
    def __init__(self, conclusion="success"):
        self.resynced = False
        self.conclusion = conclusion
        self.calls = []

    def __call__(self, gh, args):
        self.calls.append(list(args))
        if args[0] in ("pr", "repo"):
            raise RuntimeError("GraphQL: API rate limit already exceeded for user ID 1")
        path = args[-1]
        if args[:3] == ["api", "--method", "PUT"]:
            self.resynced = True
            return {"message": "Updating pull request branch."}
        sha = _SHA_B if self.resynced else _SHA_A
        if path == "repos/o/r/pulls/7":
            return {"number": 7, "state": "open", "merged": False,
                    "head": {"ref": "f", "sha": sha}, "base": {"ref": "main"}}
        if path == "repos/o/r/commits/%s/check-runs" % sha:
            return {"check_runs": [{
                "id": 11, "name": "test", "status": "completed",
                "conclusion": self.conclusion,
                "details_url": "https://github.com/o/r/actions/runs/99/job/11"}]}
        if path.startswith("repos/o/r/actions/runs?head_sha="):
            return {"workflow_runs": [{"id": 99, "name": "agent-os tests"}]}
        if path.startswith("repos/o/r/compare/"):
            return {"behind_by": 0 if self.resynced else 1}
        raise AssertionError("unexpected gh call: %r" % (args,))


def test_probe_merge_readiness_reads_over_rest_when_graphql_is_rate_limited(monkeypatch):
    fake = _GraphqlDeadGh()
    monkeypatch.setattr(pg, "_gh_json", fake)
    probe = pg.probe_merge_readiness(7, repo="o/r", gh="gh", required="agent-os tests")
    assert probe["head_sha"] == _SHA_A
    assert probe["behind_by"] == 1
    assert probe["ci"] == "success", "REST check-runs must feed the same verdict"
    assert probe["base"] == "main" and probe["state"] == "OPEN"
    assert probe["transport"] == "rest"


def test_probe_rest_leg_names_the_repo_from_git_not_gh_repo_view(monkeypatch):
    """No --repo: `gh repo view` is GraphQL too, so the repo comes from origin."""
    fake = _GraphqlDeadGh()
    monkeypatch.setattr(pg, "_gh_json", fake)
    monkeypatch.setattr(pg, "_git", lambda args, cwd=None: "https://github.com/o/r.git")
    probe = pg.probe_merge_readiness(7, gh="gh", required="agent-os tests")
    assert probe["repo"] == "o/r" and probe["head_sha"] == _SHA_A
    assert not any(c[0] == "repo" for c in fake.calls[1:]), \
        "the REST leg must never retry `gh repo view`"


def test_probe_rest_leg_still_reads_red_as_red(monkeypatch):
    """The fallback is a transport, not an amnesty: a failing run stays failure."""
    monkeypatch.setattr(pg, "_gh_json", _GraphqlDeadGh(conclusion="failure"))
    probe = pg.probe_merge_readiness(7, repo="o/r", gh="gh", required="agent-os tests")
    assert probe["ci"] == "failure"


def test_resync_and_wait_clears_the_hold_with_graphql_dead(monkeypatch):
    """The #2814 sequence end to end: GraphQL dead for the whole run, and the
    re-sync still issues its ONE update-branch and reaches GO on the new head."""
    fake = _GraphqlDeadGh()
    monkeypatch.setattr(pg, "_gh_json", fake)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(7, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=100, sleep=fc.sleep, now=fc.now)
    assert res["verdict"] == "GO", res
    puts = [c for c in fake.calls if c[:3] == ["api", "--method", "PUT"]]
    assert puts == [["api", "--method", "PUT", "repos/o/r/pulls/7/update-branch"]]


def test_resync_failed_before_the_request_says_so(monkeypatch):
    """Both transports blind on the FIRST probe: nothing was sent, and the verdict
    must carry that -- not read as GitHub refusing a request."""
    def fake_gh_json(gh, args):
        raise RuntimeError("everything is down")

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(7, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=5, sleep=fc.sleep, now=fc.now)
    assert res["verdict"] == "RESYNC_FAILED"
    assert res["stage"] == "probe"


def test_resync_failed_update_branch_refusal_is_its_own_stage(monkeypatch):
    def fake_gh_json(gh, args):
        if args[:2] == ["pr", "view"]:
            return {"number": 1, "state": "OPEN", "headRefName": "f",
                    "headRefOid": "head0", "baseRefName": "main",
                    "statusCheckRollup": _ROLLUP_GREEN}
        if args[:2] == ["api", "--method"]:
            raise RuntimeError("gh api failed: HTTP 409 merge conflict")
        return {"behind_by": 1}

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(1, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=10, sleep=fc.sleep, now=fc.now)
    assert res["stage"] == "update_branch"


def _print_resync_failed(monkeypatch, capsys, stage):
    monkeypatch.setattr(pg, "resolve", lambda *a, **k:
                        {"verdict": "STALE", "exit_code": 3, "behind_by": 1,
                         "pr": 2814, "repo": "o/r", "base": "main", "state": "OPEN"})
    monkeypatch.setattr(pg, "resync_and_wait", lambda *a, **k:
                        {"verdict": "RESYNC_FAILED", "exit_code": 3, "pr": 2814,
                         "repo": "o/r", "base": "main", "stage": stage,
                         "error": "GraphQL: API rate limit already exceeded"})
    # #3229: the resync path derives the check name first; unpinned, that is a real
    # `gh api repos/o/r/actions/workflows`.
    monkeypatch.setattr(pg, "resolve_required_check",
                        lambda *a, **k: CI)
    assert pg.main(["--pr", "2814", "--repo", "o/r", "--resync"]) == 3
    return capsys.readouterr().out


def test_probe_stage_failure_never_claims_github_refused(monkeypatch, capsys):
    out = _print_resync_failed(monkeypatch, capsys, "probe")
    assert "refused" not in out and "409" not in out, out
    assert "never sent" in out, "must say the update-branch request was not issued"
    assert "update-branch" in out, "and still name the manual one-call clear"


def test_update_branch_stage_failure_keeps_the_409_hint(monkeypatch, capsys):
    out = _print_resync_failed(monkeypatch, capsys, "update_branch")
    assert "refused the update-branch request" in out
    assert "409" in out


# ---- #3208: a refused update-branch is re-read before it is believed ----------
#
# 2026-10-22, PR #3180: a sibling session pushed `Merge branch 'main' into ...`
# 13 s after its own merge, so OUR update-branch came back HTTP 422 "expected head
# sha didn't match current head ref" -- the PR was no longer behind and its CI was
# already running on the new head. The guard held RESYNC_FAILED with a 409 hint.
# A refusal says only that OUR request did not land; whether the PR still needs a
# re-sync is a question for a fresh reading, asked once.

_REFUSED_422 = ("gh api --method PUT repos/o/r/pulls/3180/update-branch failed: "
                "gh: expected head sha didn't match current head ref. (HTTP 422)")
_REFUSED_409 = ("gh api --method PUT repos/o/r/pulls/3180/update-branch failed: "
                "gh: merge conflict between base and head (HTTP 409)")


class _RefusingGh(_FakeGh):
    """_FakeGh whose update-branch PUT is REFUSED with ``refusal``. ``on_poll`` at
    i == 0 is what landed between our first probe and our PUT."""

    def __init__(self, refusal, **kw):
        super().__init__(**kw)
        self.refusal = refusal

    def __call__(self, gh, args):
        if args[:2] == ["api", "--method"]:
            self.update_branch_calls.append(args)
            raise RuntimeError(self.refusal)
        return super().__call__(gh, args)


def test_concurrent_resync_422_waits_for_ci_on_the_current_head(monkeypatch):
    """The #3180 sequence: a sibling re-synced between our probe and our PUT.
    The re-probe reads behind_by 0, so the waiter waits for CI on the CURRENT
    head (pending -> green) and returns GO -- never re-issuing update-branch."""
    def on_poll(fake, i):
        if i == 0:   # the sibling's merge commit lands before our PUT
            fake.head_sha, fake.behind_by = "head1", 0
            fake.rollup = _ROLLUP_PENDING
        if i == 3:
            fake.rollup = _ROLLUP_GREEN

    fake = _RefusingGh(_REFUSED_422, pr=3180, head_sha="head0", behind_by=1,
                       on_poll=on_poll)
    monkeypatch.setattr(pg, "_gh_json", fake)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(3180, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=1000, sleep=fc.sleep, now=fc.now)
    assert res["verdict"] == "GO", res
    assert res["exit_code"] == 0
    assert res["head_sha"] == "head1"
    assert fake.polls >= 4, "GO only after CI concluded, not on the pending reading"
    assert len(fake.update_branch_calls) == 1, "update-branch is never re-issued"
    assert res["resync_issued"] is False, "we did not land a re-sync; a sibling did"
    assert res.get("concurrent_resync") is True
    assert "concurrent re-sync" in res.get("note", ""), res


def test_refused_update_branch_still_behind_holds_resync_failed(monkeypatch):
    """422 because the head moved (someone pushed a commit) but the PR is STILL
    behind: the re-probe says so, and the hold stands. Exactly one re-probe."""
    def on_poll(fake, i):
        if i == 0:
            fake.head_sha, fake.behind_by = "head1", 2   # pushed, still behind

    fake = _RefusingGh(_REFUSED_422, pr=3180, head_sha="head0", behind_by=1,
                       on_poll=on_poll)
    monkeypatch.setattr(pg, "_gh_json", fake)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(3180, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=1000, sleep=fc.sleep, now=fc.now)
    assert res["verdict"] == "RESYNC_FAILED", res
    assert res["exit_code"] == 3
    assert res["stage"] == "update_branch"
    assert "422" in res["error"]
    assert fake.polls == 2, "the refusal is re-read exactly once, then believed"
    assert len(fake.update_branch_calls) == 1


def test_merge_conflict_409_holds_with_the_conflict_hint(monkeypatch, capsys):
    """A genuine 409 conflict: the re-probe still reads the PR behind, so the CLI
    prints RESYNC-FAILED with the 409 hint -- end to end through main()."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k:
                        {"verdict": "STALE", "exit_code": 3, "behind_by": 1,
                         "pr": 3180, "repo": "o/r", "base": "main", "state": "OPEN"})
    fake = _RefusingGh(_REFUSED_409, pr=3180, head_sha="head0", behind_by=1)
    monkeypatch.setattr(pg, "_gh_json", fake)
    rc = pg.main(["--pr", "3180", "--repo", "o/r", "--resync",
                  "--required-check", "agent-os tests"])
    out = capsys.readouterr().out
    assert rc == 3
    assert "RESYNC-FAILED HOLD" in out and "refused the update-branch request" in out
    assert "409" in out and "merge conflict" in out, out
    assert fake.polls == 2, "the 409 is re-read once before the hold is believed"
    assert len(fake.update_branch_calls) == 1


def test_refusal_then_reprobe_error_fails_closed(monkeypatch):
    """The refusal cannot be disambiguated when the re-probe itself fails: keep
    the fail-closed RESYNC_FAILED hold, and carry the re-probe's error."""
    state = {"dead": False}

    def fake_gh_json(gh, args):
        if state["dead"]:
            raise RuntimeError("gh down")
        if args[:2] == ["pr", "view"]:
            return {"number": 3180, "state": "OPEN", "headRefName": "f",
                    "headRefOid": "head0", "baseRefName": "main",
                    "statusCheckRollup": _ROLLUP_GREEN}
        if args[:2] == ["api", "--method"]:
            state["dead"] = True
            raise RuntimeError(_REFUSED_422)
        if "/check-runs" in args[1]:
            return {"check_runs": []}
        return {"behind_by": 1}

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    fc = _FakeClock(step=1.0)
    res = pg.resync_and_wait(3180, repo="o/r", gh="gh", required="agent-os tests",
                             timeout=1000, sleep=fc.sleep, now=fc.now)
    assert res["verdict"] == "RESYNC_FAILED", res
    assert res["exit_code"] == 3
    assert res["stage"] == "update_branch"
    assert "422" in res["error"]
    assert "gh down" in res.get("reprobe_error", ""), res


# ---- main() wiring: --resync is opt-in and only fires on a STALE verdict ------

def test_main_stale_without_resync_flag_does_not_touch_the_pr(monkeypatch):
    """Default behaviour is unchanged: STALE prints the manual one-liner and never
    issues a write. Automating a write on every invocation of a read-only gate
    would be a surprise, so it stays opt-in."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k:
                        {"verdict": "STALE", "exit_code": 3, "behind_by": 1,
                         "pr": 2109, "repo": "o/r", "base": "main", "state": "OPEN"})

    def must_not_call(*a, **k):
        raise AssertionError("resync_and_wait fired without --resync")

    monkeypatch.setattr(pg, "resync_and_wait", must_not_call)
    assert pg.main(["--pr", "2109", "--repo", "o/r"]) == 3


def test_main_resync_flag_clears_the_hold_and_rechecks(monkeypatch):
    """--resync on a STALE verdict runs the waiter, and on its GO RE-RESOLVES the
    base rather than trusting the waiter's exit reading.

    'Re-check the guard at merge time, not at watch-exit time' -- the gap between
    those two moments is where fire 4 happened."""
    seen = {"resolves": 0, "resync": 0}

    def fake_resolve(*a, **k):
        seen["resolves"] += 1
        if seen["resolves"] == 1:
            return {"verdict": "STALE", "exit_code": 3, "behind_by": 1,
                    "pr": 2109, "repo": "o/r", "base": "main", "state": "OPEN"}
        return {"verdict": "GO", "exit_code": 0, "behind_by": 0,
                "pr": 2109, "repo": "o/r", "base": "main", "state": "OPEN"}

    def fake_resync(*a, **k):
        seen["resync"] += 1
        return {"verdict": "GO", "exit_code": 0, "pr": 2109, "ci": "success"}

    monkeypatch.setattr(pg, "resolve", fake_resolve)
    monkeypatch.setattr(pg, "resync_and_wait", fake_resync)
    monkeypatch.setattr(pg, "merge_time_duplicate", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0})
    monkeypatch.setattr(pg, "file_overlap_advisory", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "overlaps": []})
    monkeypatch.setattr(pg, "resolve_required_check",
                        lambda *a, **k: CI)
    monkeypatch.setattr(pg, "green_gate", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "ci": "success"})
    assert pg.main(["--pr", "2109", "--repo", "o/r", "--resync"]) == 0
    assert seen["resync"] == 1
    assert seen["resolves"] == 2, "the base must be re-resolved after the wait"


def test_main_resync_hold_is_reported_not_swallowed(monkeypatch):
    """A waiter HOLD is the answer -- main must not fall through to the old
    STALE text and must not re-resolve into a GO."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k:
                        {"verdict": "STALE", "exit_code": 3, "behind_by": 1,
                         "pr": 2109, "repo": "o/r", "base": "main", "state": "OPEN"})
    monkeypatch.setattr(pg, "resync_and_wait", lambda *a, **k:
                        {"verdict": "BASE_MOVED", "exit_code": 3, "pr": 2109,
                         "behind_by": 1, "base": "main"})
    monkeypatch.setattr(pg, "resolve_required_check",       # #3229: else a real gh api call
                        lambda *a, **k: CI)
    assert pg.main(["--pr", "2109", "--repo", "o/r", "--resync"]) == 3


def test_stale_message_names_the_one_command_clear(capsys, monkeypatch):
    """Surface-to-clear (#2024, extended by #2117): the hold text must now name
    the flag that clears it in one step, not only the raw API call."""
    monkeypatch.setattr(pg, "resolve", lambda *a, **k:
                        {"verdict": "STALE", "exit_code": 3, "behind_by": 2,
                         "pr": 2109, "repo": "o/r", "base": "main", "state": "OPEN"})
    pg.main(["--pr", "2109", "--repo", "o/r"])
    out = capsys.readouterr().out
    assert "--resync" in out, "the hold must name its own one-command clear"


# ---- #2150: second transport for behind_by ---------------------------------
#
# The compare REST endpoint is the guard's ONLY instrument for base-freshness.
# On 2026-08-17 it 404'd for ~4.5h on private repos (while `pulls`/`contents`
# answered normally and git protocol was unaffected), which turned every `mgo`
# in the repo into a refusal: resolve() raises -> main() returns UNDETERMINED(2)
# -> preflight_auth.py treats any non-zero as do-not-arm.
#
# These pin the fallback AND, more importantly, the conditions under which it
# must REFUSE to answer. A fallback that silently measures the wrong repo or a
# stale local ref is worse than UNDETERMINED: it hands the arming path a
# confident wrong number, and nothing downstream can tell it apart from a real
# reading.

def test_behind_via_git_measures_over_git_protocol(monkeypatch):
    """Happy path: origin matches the target repo, so the count is trusted."""
    calls = []

    def fake_git(args, cwd=None):
        calls.append(args)
        if args[0] == "remote":
            return "git@github.com:octo/home-repo.git"
        if args[0] == "fetch":
            return ""
        if args[0] == "rev-list":
            return "3"
        raise AssertionError("unexpected git call: %r" % (args,))

    monkeypatch.setattr(pg, "_git", fake_git)
    assert pg.behind_via_git("octo/home-repo", "main", "topic") == 3
    assert any(a[0] == "fetch" for a in calls), "must refresh refs before counting"
    revlist = [a for a in calls if a[0] == "rev-list"][0]
    assert "refs/remotes/origin/topic..refs/remotes/origin/main" in revlist, (
        "behind_by is base-not-in-head: count must be head..base, not base..head")


def test_behind_via_git_refuses_when_local_repo_is_a_different_repo(monkeypatch):
    """--repo can name a repo the cwd checkout is NOT. Measuring the local one
    would return a confident wrong answer, so the fallback must decline."""
    seen = []

    def fake_git(args, cwd=None):
        seen.append(args[0])
        if args[0] == "remote":
            return "git@github.com:octo/notes.git"
        return "0"

    monkeypatch.setattr(pg, "_git", fake_git)
    assert pg.behind_via_git("octo/home-repo", "main", "topic") is None
    # Load-bearing: the refusal must happen BEFORE any measurement. If the
    # identity check were removed, these calls would run against the wrong
    # repo's graph and return a confident wrong number -- the failure mode this
    # whole function is shaped to avoid.
    assert "fetch" not in seen and "rev-list" not in seen, (
        "must decline before measuring, not measure then discard")


def test_behind_via_git_refuses_when_no_origin(monkeypatch):
    def boom(args, cwd=None):
        raise RuntimeError("no origin")
    monkeypatch.setattr(pg, "_git", boom)
    assert pg.behind_via_git("octo/home-repo", "main", "topic") is None


def test_behind_via_git_refuses_when_fetch_fails(monkeypatch):
    """Fork head refs do not exist on origin -- fetch fails, so do not guess."""
    def fake_git(args, cwd=None):
        if args[0] == "remote":
            return "https://github.com/octo/home-repo"
        raise RuntimeError("couldn't find remote ref refs/heads/topic")
    monkeypatch.setattr(pg, "_git", fake_git)
    assert pg.behind_via_git("octo/home-repo", "main", "topic") is None


def test_nwo_parsing_handles_both_url_forms():
    assert pg._nwo_from_url("git@github.com:o/r.git") == "o/r"
    assert pg._nwo_from_url("https://github.com/o/r.git") == "o/r"
    assert pg._nwo_from_url("https://github.com/o/r") == "o/r"
    assert pg._nwo_from_url("") is None


def test_resolve_falls_back_when_compare_probe_fails(monkeypatch):
    """The 2026-08-17 outage, reproduced: compare 404s, everything else answers.
    The guard must still return a real verdict instead of raising."""
    def fake_gh_json(gh, args):
        if args[0] == "pr":
            return {"number": 2144, "state": "OPEN",
                    "headRefName": "topic", "baseRefName": "main"}
        if args[0] == "api" and "compare" in args[1]:
            raise RuntimeError("gh api ... failed: gh: Not Found (HTTP 404)")
        raise AssertionError("unexpected gh call: %r" % (args,))

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    monkeypatch.setattr(pg, "behind_via_git", lambda *a, **k: 0)
    res = pg.resolve(2144, repo="o/r")
    assert res["verdict"] == "GO"
    assert res["exit_code"] == pg.GO
    assert res.get("probe") == "git", "the verdict must record which transport answered"


def test_resolve_fallback_still_holds_a_stale_base(monkeypatch):
    """The fallback is a second way to SEE, not a way to pass. A stale base
    measured over git protocol must hold exactly as the REST reading would."""
    def fake_gh_json(gh, args):
        if args[0] == "pr":
            return {"number": 7, "state": "OPEN",
                    "headRefName": "topic", "baseRefName": "main"}
        raise RuntimeError("gh: Not Found (HTTP 404)")

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    monkeypatch.setattr(pg, "behind_via_git", lambda *a, **k: 2)
    res = pg.resolve(7, repo="o/r")
    assert res["verdict"] == "STALE"
    assert res["exit_code"] == pg.STALE_HOLD
    assert res["behind_by"] == 2


def test_resolve_reraises_when_fallback_also_declines(monkeypatch):
    """Both transports blind => UNDETERMINED is preserved. The fallback must
    never convert 'cannot tell' into 'go ahead'."""
    def fake_gh_json(gh, args):
        if args[0] == "pr":
            return {"number": 7, "state": "OPEN",
                    "headRefName": "topic", "baseRefName": "main"}
        raise RuntimeError("gh: Not Found (HTTP 404)")

    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)
    monkeypatch.setattr(pg, "behind_via_git", lambda *a, **k: None)
    with pytest.raises(Exception):
        pg.resolve(7, repo="o/r")


def test_main_still_exits_undetermined_when_both_transports_blind(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("gh: Not Found (HTTP 404)")
    monkeypatch.setattr(pg, "resolve", boom)
    assert pg.main(["--pr", "7", "--repo", "o/r", "--json"]) == pg.UNDETERMINED


def test_behind_via_git_counts_correctly_against_real_git(tmp_path):
    """Behavioural backstop: the refspec/direction above is asserted against a
    fake, so pin the actual counting semantics against real git once."""
    import subprocess as sp

    def git(*args, cwd):
        return sp.run(["git", *args], cwd=str(cwd), capture_output=True,
                      text=True, check=True).stdout.strip()

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    sp.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    sp.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "t", cwd=work)
    git("config", "commit.gpgsign", "false", cwd=work)
    (work / "f.txt").write_text("1\n")
    git("add", "f.txt", cwd=work)
    git("commit", "-qm", "c1", cwd=work)
    git("branch", "-M", "main", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    git("checkout", "-qb", "topic", cwd=work)
    git("push", "-q", "origin", "topic", cwd=work)
    # main advances twice; topic stays put -> topic is 2 behind main.
    git("checkout", "-q", "main", cwd=work)
    (work / "f.txt").write_text("2\n")
    git("commit", "-qam", "c2", cwd=work)
    (work / "f.txt").write_text("3\n")
    git("commit", "-qam", "c3", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)

    # Point the fallback at this checkout, with an origin that "matches".
    real_git = pg._git

    def scoped_git(args, cwd=None):
        if args[0] == "remote":
            return "git@github.com:o/r.git"
        return real_git(args, cwd=str(work))

    saved = pg._git
    try:
        pg._git = scoped_git
        assert pg.behind_via_git("o/r", "main", "topic", cwd=str(work)) == 2
    finally:
        pg._git = saved


def test_finding1_undetermined_comment_names_the_arming_boundary():
    """#2150 Finding 1: the in-file docs claimed exit 2 'never blocks a merge'
    and that callers hold 'ONLY on exit 3'. Both are false at the only path
    that arms the merge marker (hooks/preflight_auth.py), so a reader --
    human or agent -- re-derived a wrong conclusion from live-looking text."""
    src = open(_GUARD, encoding="utf-8").read()
    assert "never blocks a merge" not in src, (
        "the false fail-open claim must not survive in the source")
    assert "preflight_auth" in src, (
        "the exit-2 docs must name the arming boundary that fails closed on it")


# ---- #2232: the rollup lags the head SHA (inherited-success false GO) ---------
# `--resync` cleared the STALE hold and then handed back the human round-trip
# #2117 existed to remove: it returned GO within seconds, and the green gate --
# which polls fresh -- immediately HELD on a pending run. Deduced control flow
# (issue #2232): `evaluate_resync` saw `ci == "success"` for the POST-resync SHA
# at a moment when that SHA's own run was still queued.
#
# The cause is that `gh pr view` is ONE response but not ONE reading: GitHub's
# statusCheckRollup is denormalized and, for a window after `update-branch`, it
# serves the PREVIOUS head's concluded runs alongside the NEW headRefOid. Probing
# both in a single call binds them syntactically, not semantically.
#
# #2232 proposed matching each check run's own `headSha`. That field does NOT
# exist on a statusCheckRollup entry (verified against live output 2026-08-21:
# __typename/name/status/conclusion/detailsUrl/workflowName/startedAt/completedAt
# and nothing else), so the fix is the same idea via the join key that IS there:
# `detailsUrl` carries the Actions run id, and `repos/{repo}/commits/{sha}/
# check-runs` is SHA-addressed by construction. Intersecting the two binds the
# rollup to the SHA semantically.

_OLD_RUN, _NEW_RUN = "32471183143", "32480000001"


def _rollup_from_run(run_id, status="COMPLETED", conclusion="SUCCESS"):
    return [{"__typename": "CheckRun", "workflowName": "agent-os tests",
             "name": "test", "status": status, "conclusion": conclusion,
             "context": None, "state": None,
             "detailsUrl":
                 "https://github.com/o/r/actions/runs/%s/job/96738091884" % run_id}]


class _LaggingRollupGh(_FakeGh):
    """GitHub in the window right after `update-branch`: `pr view` already reports
    the NEW head SHA while its rollup still serves the PREVIOUS head's runs.

    ``head_run_ids`` is what the SHA-addressed check-runs endpoint attributes to
    the probed head -- empty means "this head has no runs registered yet", which
    is the exact moment #2232 fired."""

    def __init__(self, head_run_ids=(), head_run_status="QUEUED", **kw):
        super().__init__(**kw)
        self.head_run_ids = list(head_run_ids)
        self.head_run_status = head_run_status
        self.check_run_paths = []

    def __call__(self, gh, args):
        if args[0] == "api" and "/check-runs" in args[1]:
            self.check_run_paths.append(args[1])
            return {"check_runs": [
                {"name": "test", "status": self.head_run_status,
                 "conclusion": None,
                 "details_url":
                     "https://github.com/o/r/actions/runs/%s/job/1" % run_id}
                for run_id in self.head_run_ids]}
        return super().__call__(gh, args)


def test_probe_drops_rollup_entries_not_attached_to_the_probed_head(monkeypatch):
    """THE #2232 test. The rollup's only matching entry is a CONCLUDED SUCCESS
    belonging to the previous head's run; the probed SHA's own run is still queued.

    A success that belongs to another commit is not evidence about this one, so it
    must not survive into the probe's `ci` reading."""
    fake = _LaggingRollupGh(head_sha="head1", behind_by=0,
                            rollup=_rollup_from_run(_OLD_RUN),
                            head_run_ids=[_NEW_RUN])
    monkeypatch.setattr(pg, "_gh_json", fake)
    probe = pg.probe_merge_readiness(2229, repo="o/r", gh="gh",
                                     required="agent-os tests")
    assert probe["ci"] != "success", (
        "an inherited run's SUCCESS must not read as this head's green")
    assert fake.check_run_paths == ["repos/o/r/commits/head1/check-runs"], (
        "the binding query must be SHA-addressed, not branch-addressed")


def test_inherited_success_keeps_the_resync_waiting(monkeypatch):
    """End-to-end shape of the bug: GO here is what turned a 1800s budget into
    ~0s and handed the operator back the round-trip. It must WAIT instead."""
    fake = _LaggingRollupGh(head_sha="head1", behind_by=0,
                            rollup=_rollup_from_run(_OLD_RUN),
                            head_run_ids=[_NEW_RUN])
    monkeypatch.setattr(pg, "_gh_json", fake)
    probe = pg.probe_merge_readiness(2229, repo="o/r", gh="gh",
                                     required="agent-os tests")
    assert pg.evaluate_resync(probe, "head0")["verdict"] == "WAITING"


def test_probe_keeps_a_success_attached_to_the_probed_head(monkeypatch):
    """Non-vacuity (zero-metric gate): the filter must still let a REAL green
    through. A guard that only ever suppresses would pass the test above while
    making `--resync` incapable of ever returning GO."""
    fake = _LaggingRollupGh(head_sha="head1", behind_by=0,
                            rollup=_rollup_from_run(_NEW_RUN),
                            head_run_ids=[_NEW_RUN])
    monkeypatch.setattr(pg, "_gh_json", fake)
    probe = pg.probe_merge_readiness(2229, repo="o/r", gh="gh",
                                     required="agent-os tests")
    assert probe["ci"] == "success"
    assert pg.evaluate_resync(probe, "head0")["verdict"] == "GO"


def test_probe_keeps_entries_that_carry_no_run_id(monkeypatch):
    """Legacy StatusContext entries have no Actions run id, so the intersection
    cannot judge them. Dropping them would silently mute a whole class of required
    check; they pass through unfiltered."""
    legacy = [{"__typename": "StatusContext", "context": "agent-os tests",
               "state": "SUCCESS", "targetUrl": "https://ci.example/1",
               "workflowName": None, "name": None, "status": None}]
    fake = _LaggingRollupGh(head_sha="head1", behind_by=0, rollup=legacy,
                            head_run_ids=[_NEW_RUN])
    monkeypatch.setattr(pg, "_gh_json", fake)
    probe = pg.probe_merge_readiness(2229, repo="o/r", gh="gh",
                                     required="agent-os tests")
    assert probe["ci"] == "success"


def test_run_id_extraction_survives_url_shapes():
    assert pg._run_id({"detailsUrl":
                       "https://github.com/o/r/actions/runs/123/job/456"}) == "123"
    assert pg._run_id({"details_url":
                       "https://github.com/o/r/actions/runs/789"}) == "789"
    assert pg._run_id({"targetUrl": "https://ci.example/build/5"}) is None
    assert pg._run_id({}) is None


# --- #2176: the UNDETERMINED stderr text must not claim a block it cannot make ---
#
# Both UNDETERMINED messages asserted "This DOES block an `mgo`" unconditionally.
# That is true for ONE of the two callers and false for the other:
#   - hooks/preflight_auth.py (the `mgo <N>` PROMPT path) fails CLOSED: "non-zero
#     means do not arm" -- the claim holds.
#   - hooks/merge_go.py (the direct `! mgo <N>` path) treats exit 2 as this guard's
#     OWN fail-open verdict, prints a warning and RETURNS, i.e. proceeds to merge
#     (merge_go.py:165-169) -- the claim is false, and it is printed from here.
# Live on 2026-08-19 the line printed immediately above a successful merge.
# These pin the message against re-acquiring the unconditional claim.
#
# In the kit the message names the two caller behaviours, not files: the kit has
# the phrase arm (merges only on exit 0) and no direct-merge wrapper, so naming
# hooks/merge_go.py would point a user at a file the kit does not ship.


def _undetermined_stderr(monkeypatch, capsys, resync=False):
    """Drive main() down a both-transports-blind path and return its stderr."""
    def boom(*a, **k):
        raise RuntimeError("gh: Not Found (HTTP 404)")
    monkeypatch.setattr(pg, "resolve", boom)
    assert pg.main(["--pr", "7", "--repo", "o/r"]) == pg.UNDETERMINED
    return capsys.readouterr().err


def test_undetermined_message_does_not_claim_an_unconditional_block(monkeypatch,
                                                                   capsys):
    err = _undetermined_stderr(monkeypatch, capsys)
    assert "UNDETERMINED" in err
    assert "DOES block an" not in err


def test_undetermined_message_names_the_direct_bash_fail_open_path(monkeypatch,
                                                                  capsys):
    err = _undetermined_stderr(monkeypatch, capsys)
    assert "only on exit 0" in err and "is blocked" in err
    assert "fail-open" in err and "PROCEEDS TO MERGE" in err
    assert "merge_go.py" not in err and "hooks/" not in err


def test_undetermined_message_still_names_the_two_clearing_probes(monkeypatch,
                                                                 capsys):
    """The actionable half must survive the correction (no regression)."""
    err = _undetermined_stderr(monkeypatch, capsys)
    assert "git fetch origin" in err
    assert "compare" in err


def test_post_resync_undetermined_message_also_names_both_paths(monkeypatch,
                                                               capsys):
    """The twin site behind --resync carried the identical false claim."""
    calls = {"n": 0}

    def resolve_then_boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"verdict": "STALE", "exit_code": pg.STALE_HOLD, "pr": 7,
                    "base": "main", "repo": "o/r", "repo_inferred": False}
        raise RuntimeError("gh: Not Found (HTTP 404)")

    monkeypatch.setattr(pg, "resolve", resolve_then_boom)
    monkeypatch.setattr(pg, "resolve_required_check", lambda *a, **k: "agent-os tests")
    monkeypatch.setattr(pg, "resync_and_wait",
                        lambda *a, **k: {"verdict": "GO", "exit_code": pg.GO,
                                         "pr": 7, "base": "main"})
    assert pg.main(["--pr", "7", "--repo", "o/r", "--resync"]) == pg.UNDETERMINED
    err = capsys.readouterr().err
    assert "DOES block an" not in err
    assert "only on exit 0" in err and "PROCEEDS TO MERGE" in err
    assert "merge_go.py" not in err and "hooks/" not in err


# ============================================================================
# #2583 -- the CI-green probe must survive an exhausted GraphQL bucket.
#
# `fetch_check_rollup` reads `gh pr view --json statusCheckRollup`, measured
# graphql=1/rest=0 (#2549 direction c, ref/workflow.md). When the account's
# shared 5000/hr GraphQL bucket is spent -- routinely, with two or three
# concurrent sessions -- the merge gate cannot see CI at all and fails closed on
# a PR that is green, 0-behind and mergeable. Measured 2026-09-05: three `Mgo`
# refusals on PR #2575, the third being exactly this; the owner merged by hand.
#
# THE TRAP these tests exist to catch (issue #2583, verbatim): the REST
# check-runs payload has NO `workflowName` key -- only job names (`test`,
# `labels`, `advance-deploy-ref`). `_match_check` gates on the WORKFLOW name
# ("agent-os tests"), so an adapter that forgets the workflow-name join matches
# NOTHING, returns `absent`, and HOLDs anyway -- a fallback that looks like it
# works and changes nothing. `test_rest_join_is_load_bearing_*` below is the
# guard: it asserts the join happens AND that its absence flips the verdict.
# ============================================================================

_FIX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "premerge-guard")


def _fixture(name):
    with open(os.path.join(_FIX_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


# Verbatim `GET /repos/{nwo}/commits/{sha}/check-runs` for PR #2579's head
# f998952b (2026-09-05), unslimmed. Carries the shape AC2 names: `labels`
# CANCELLED (id 101298030067) superseded by `labels` SUCCESS (id 101298085307).
_CHECK_RUNS_F998 = _fixture("check-runs-f998952b.json")
# The workflow-name join for the same SHA; slimmed to the joined fields, values
# verbatim (see its own `_note`). Run 33962989948 == "agent-os tests".
_ACTIONS_RUNS_F998 = _fixture("actions-runs-f998952b.json")


class _RestGH:
    """Stands in for `_gh_json`, answering only the REST endpoints the fallback
    uses. Any `pr view` (GraphQL) raises the live rate-limit error verbatim."""

    def __init__(self, check_runs=None, actions_runs=None, sha=None,
                 fail_check_runs=False, fail_actions_runs=False):
        self.check_runs = _CHECK_RUNS_F998 if check_runs is None else check_runs
        self.actions_runs = _ACTIONS_RUNS_F998 if actions_runs is None else actions_runs
        self.sha = sha or "f998952bdd37b7cbbcedbd4706f567cbb35a95f9"
        self.fail_check_runs = fail_check_runs
        self.fail_actions_runs = fail_actions_runs
        self.calls = []

    def __call__(self, gh, args):
        self.calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            raise RuntimeError(
                "gh pr view failed: GraphQL: API rate limit already exceeded "
                "for user ID 33196704.")
        path = args[-1] if args and args[0] == "api" else ""
        if "/pulls/" in path:
            return {"number": 2579, "state": "open", "head": {"sha": self.sha}}
        if "/check-runs" in path:
            if self.fail_check_runs:
                raise RuntimeError("REST check-runs 502")
            return self.check_runs
        if "/actions/runs" in path:
            if self.fail_actions_runs:
                raise RuntimeError("REST actions/runs 502")
            # Behave like the real endpoint: `head_sha` matches only the FULL
            # 40-char SHA and returns an EMPTY set for an abbreviated one, with
            # no error (measured 2026-09-05 against f998952b / eb6313f4). This
            # is what makes a short-SHA implementation reddening rather than
            # silently reopening the always-`absent` trap.
            asked = path.split("head_sha=", 1)[1] if "head_sha=" in path else ""
            if asked != self.sha:
                return {"total_count": 0, "workflow_runs": []}
            return self.actions_runs
        raise AssertionError("unexpected gh call: %r" % (args,))

    def paths(self):
        return [a[-1] for a in self.calls if a and a[0] == "api"]


# ---- AC2: the adapter, against the verbatim fixture --------------------------

def test_rest_adapter_joins_job_names_to_their_workflow_name(monkeypatch):
    monkeypatch.setattr(pg, "_gh_json", _RestGH())
    rollup = pg.fetch_rollup_via_rest(2579, repo="octo/home-repo")
    by_name = {c["name"]: c for c in rollup}
    assert by_name["test"]["workflowName"] == "agent-os tests"
    assert by_name["advance-deploy-ref"]["workflowName"] == "agent-os tests"
    # `labels` is a DIFFERENT workflow and must not be dragged into the gate.
    assert by_name["labels"]["workflowName"] == "PR label guard"


def test_rest_adapter_keeps_only_the_newest_run_per_name(monkeypatch):
    """AC2: the fixture carries `labels` CANCELLED (101298030067) superseded by
    `labels` SUCCESS (101298085307). check-runs is SHA-addressed and keeps both;
    reading them together would score a superseded cancel as a live failure."""
    raw = [c for c in _CHECK_RUNS_F998["check_runs"] if c["name"] == "labels"]
    assert {c["conclusion"] for c in raw} == {"cancelled", "success"}, \
        "fixture must carry the superseded pair -- re-capture it if this fails"
    monkeypatch.setattr(pg, "_gh_json", _RestGH())
    rollup = pg.fetch_rollup_via_rest(2579, repo="octo/home-repo")
    labels = [c for c in rollup if c["name"] == "labels"]
    assert len(labels) == 1
    assert labels[0]["conclusion"] == "success"


def test_rest_adapter_uses_the_full_sha_for_the_workflow_join(monkeypatch):
    """The `head_sha` filter SILENTLY returns zero rows for an abbreviated SHA
    (measured 2026-09-05: 8-char -> total_count 0; 40-char -> 2 runs). A short
    SHA here would empty the join map and re-open the always-`absent` trap."""
    gh = _RestGH()
    monkeypatch.setattr(pg, "_gh_json", gh)
    pg.fetch_rollup_via_rest(2579, repo="octo/home-repo")
    joins = [p for p in gh.paths() if "/actions/runs" in p]
    assert joins, "the adapter never issued the workflow-name join"
    assert "f998952bdd37b7cbbcedbd4706f567cbb35a95f9" in joins[0]


def test_rest_adapter_refuses_a_short_head_sha_loudly(monkeypatch):
    """If the PR payload ever yields an abbreviated SHA, RAISE rather than join
    against it: `head_sha` would match nothing, the map would be empty, and the
    gate would HOLD a green PR for a reason no message explains. Fail closed with
    a message that names the cause."""
    monkeypatch.setattr(pg, "_gh_json", _RestGH(sha="f998952b"))
    with pytest.raises(Exception) as exc:
        pg.fetch_rollup_via_rest(2579, repo="octo/home-repo")
    assert "40-char" in str(exc.value)


def test_rest_adapter_never_issues_a_graphql_call(monkeypatch):
    gh = _RestGH()
    monkeypatch.setattr(pg, "_gh_json", gh)
    pg.fetch_rollup_via_rest(2579, repo="octo/home-repo")
    assert all(a[0] == "api" for a in gh.calls), gh.calls
    assert not any("graphql" in " ".join(a) for a in gh.calls)


# ---- AC5's trap, pinned in both directions ----------------------------------

def test_rest_join_is_load_bearing_for_the_required_check(monkeypatch):
    """The whole point. WITH the join the required workflow is found and green;
    WITHOUT it (join map empty) `_match_check` sees only job names, matches
    nothing, and `evaluate_checks` returns `absent` -> HOLD. Both directions are
    asserted so a broken join cannot pass as a working fallback."""
    monkeypatch.setattr(pg, "_gh_json", _RestGH())
    joined = pg.fetch_rollup_via_rest(2579, repo="octo/home-repo")
    assert pg.evaluate_checks(joined, "agent-os tests") == "success"

    monkeypatch.setattr(pg, "_gh_json", _RestGH(fail_actions_runs=True))
    unjoined = pg.fetch_rollup_via_rest(2579, repo="octo/home-repo")
    assert pg.evaluate_checks(unjoined, "agent-os tests") == "absent", (
        "without the workflow-name join the gate is structurally unable to find "
        "its required check -- this is the #2583 no-op trap")


def test_a_failed_join_degrades_to_job_names_not_to_a_crash(monkeypatch):
    """The join is best-effort: if `/actions/runs` is down the adapter still
    returns the runs (matchable by JOB name, e.g. --required-check test), rather
    than raising and collapsing the whole fallback."""
    monkeypatch.setattr(pg, "_gh_json", _RestGH(fail_actions_runs=True))
    rollup = pg.fetch_rollup_via_rest(2579, repo="octo/home-repo")
    assert {c["name"] for c in rollup} >= {"test", "labels", "advance-deploy-ref"}
    assert pg.evaluate_checks(rollup, "test") == "success"


# ---- AC1 + AC4: green_gate falls back, and says which transport answered ------

def test_green_gate_falls_back_to_rest_when_graphql_is_rate_limited(monkeypatch):
    """AC1: the exact 2026-09-05 shape -- GraphQL exhausted, REST healthy, PR
    green. Before #2583 this returned NOT_GREEN/undetermined and blocked a
    mergeable PR."""
    monkeypatch.setattr(pg, "_gh_json", _RestGH())
    res = pg.green_gate(2579, repo="octo/home-repo",
                        required="agent-os tests")
    assert res["verdict"] == "GO"
    assert res["exit_code"] == pg.GO
    assert res["ci"] == "success"


def test_green_gate_names_the_transport_that_answered(monkeypatch):
    """AC4: an operator must be able to tell a quota event from a CI problem."""
    monkeypatch.setattr(pg, "_gh_json", _RestGH())
    res = pg.green_gate(2579, repo="octo/home-repo",
                        required="agent-os tests")
    assert res["transport"] == "rest"

    monkeypatch.setattr(pg, "fetch_check_rollup", lambda *a, **k: _ROLLUP_1735_GREEN)
    res2 = pg.green_gate(1735, repo="o/r", required=CI)
    assert res2["transport"] == "graphql"


def test_green_gate_still_holds_on_a_red_rest_reading(monkeypatch):
    """The fallback must not become a way to wave red CI through."""
    red = json.loads(json.dumps(_CHECK_RUNS_F998))
    for run in red["check_runs"]:
        if run["name"] == "test":
            run["conclusion"] = "failure"
    monkeypatch.setattr(pg, "_gh_json", _RestGH(check_runs=red))
    res = pg.green_gate(2579, repo="octo/home-repo",
                        required="agent-os tests")
    assert res["exit_code"] == pg.GREEN_HOLD
    assert res["ci"] == "failure"
    assert [f["name"] for f in res["failing"]] == ["test"]


# ---- AC3: both transports blind is still UNDETERMINED ------------------------

def test_both_transports_blind_still_holds_undetermined(monkeypatch):
    """AC3: fail-closed is unchanged (#1736). The error must name BOTH failures,
    or an operator fixing the GraphQL quota never learns REST was down too."""
    monkeypatch.setattr(pg, "_gh_json", _RestGH(fail_check_runs=True))
    fc = _FakeClock(step=1.0)
    res = pg.green_gate(2579, repo="octo/home-repo",
                        required="agent-os tests", wait_green=5,
                        sleep=fc.sleep, now=fc.now)
    assert res["verdict"] == "NOT_GREEN"
    assert res["exit_code"] == pg.GREEN_HOLD == 3
    assert res["ci"] == "undetermined"
    assert "rate limit" in res["error"] and "502" in res["error"]


# ---- #2748: a non-OPEN PR prints a DISTINCT verdict and exits NOOP, never GO --
# 2026-09-11: PR #2737 merged at 14:14:23Z. For the next five hours every
# `premerge_guard.py --pr 2737` printed `GO -- PR #2737 is up to date with main;
# CI green is trustworthy.` -- byte-identical to an open, ready PR -- so the
# operator sent `Mgo 2737` three times, one of which armed a one-shot marker
# for a merge that could not happen, and a `--resync` was spent on it too. The
# #2733 class one tool over: a verdict line that reads identically in two
# states that take different actions. `resolve()` was right that there is
# nothing to GATE on a merged PR; it was wrong to call that GO, because every
# consumer upstream of the human reads GO as "merge this".

# `gh pr view 2737 --json number,state,headRefName,baseRefName,mergedAt,
# mergeCommit,closedAt` verbatim, 2026-09-11.
_MERGED_2737 = {"number": 2737, "state": "MERGED",
                "headRefName": "fix/2733-dup-scan-verdict-self-describing",
                "baseRefName": "main", "mergedAt": "2026-09-11T14:14:23Z",
                "mergeCommit": {"oid": "f229afac3aeea692cdfe9905e38d4097b9e15b8d"},
                "closedAt": "2026-09-11T14:14:23Z"}


def _stub_non_open(monkeypatch, view):
    """`pr view` answers `view`; every OTHER gh call fails the test -- a non-OPEN
    PR must reach no compare / dup / green / overlap / resync probe."""
    def fake_gh_json(gh, args):
        if args[0] == "pr" and args[1] == "view":
            return dict(view)
        raise AssertionError("a non-OPEN PR must run no further probe: %r" % (args,))
    monkeypatch.setattr(pg, "_gh_json", fake_gh_json)

    def must_not(*a, **k):
        raise AssertionError("a later gate ran for a non-OPEN PR")
    monkeypatch.setattr(pg, "resolve_required_check", must_not)
    monkeypatch.setattr(pg, "green_gate", must_not)
    monkeypatch.setattr(pg, "merge_time_duplicate", must_not)
    monkeypatch.setattr(pg, "file_overlap_advisory", must_not)
    monkeypatch.setattr(pg, "resync_and_wait", must_not)


def test_main_already_merged_prints_distinct_line_and_exits_noop(monkeypatch, capsys):
    """The measured case: the line must not START with GO (that is the token the
    `Mgo` chain keys on), must name the merge time + sha, and must exit NOOP."""
    _stub_non_open(monkeypatch, _MERGED_2737)
    rc = pg.main(["--pr", "2737", "--repo", "octo/home-repo"])
    out = capsys.readouterr().out
    assert rc == pg.NOOP == 5
    assert not out.startswith("GO"), out
    assert out.startswith("ALREADY MERGED -- PR #2737 merged at 2026-09-11T14:14:23Z "
                          "(f229afac); nothing to merge"), out
    assert "CI green is trustworthy" not in out


def test_main_already_merged_json_carries_verdict_state_and_merge_fields(monkeypatch,
                                                                         capsys):
    """The --json shape is what hooks/preflight_auth.py relays verbatim, so it
    must say ALREADY_MERGED in its own words, not `GO` + a `note` nobody prints."""
    _stub_non_open(monkeypatch, _MERGED_2737)
    rc = pg.main(["--pr", "2737", "--repo", "octo/home-repo", "--json"])
    res = json.loads(capsys.readouterr().out)
    assert rc == pg.NOOP
    assert res["verdict"] == "ALREADY_MERGED"
    assert res["exit_code"] == pg.NOOP
    assert res["state"] == "MERGED"
    assert res["merged_at"] == "2026-09-11T14:14:23Z"
    assert res["merge_commit"] == "f229afac3aeea692cdfe9905e38d4097b9e15b8d"
    assert res["repo"] == "octo/home-repo"


def test_main_already_closed_unmerged_prints_closed_line(monkeypatch, capsys):
    closed = dict(_MERGED_2737, number=2700, state="CLOSED", mergedAt=None,
                  mergeCommit=None, closedAt="2026-09-10T08:00:00Z")
    _stub_non_open(monkeypatch, closed)
    rc = pg.main(["--pr", "2700", "--repo", "o/r"])
    out = capsys.readouterr().out
    assert rc == pg.NOOP
    assert not out.startswith("GO"), out
    assert out.startswith("ALREADY CLOSED -- PR #2700 was closed without merging at "
                          "2026-09-10T08:00:00Z; nothing to merge"), out


def test_main_resync_on_a_merged_pr_never_resyncs(monkeypatch, capsys):
    """The incident spent a `--resync` on the merged PR. Only a STALE verdict may
    trigger the server-side update-branch; NOOP must not."""
    _stub_non_open(monkeypatch, _MERGED_2737)
    rc = pg.main(["--pr", "2737", "--repo", "octo/home-repo", "--resync"])
    assert rc == pg.NOOP
    assert capsys.readouterr().out.startswith("ALREADY MERGED")


# ---- #3237: the re-sync lease ----------------------------------------------
# A lap is an update-branch plus a ~30-min CI wait on the shared runner. On
# 2026-10-22 a sibling session's merge landed inside 2 of #3228's 4 laps
# (dfcc1882 19 min in, 909e6d42 7 min in) and BASE-MOVED each. --resync now
# holds a lease (merge_lock.py) from its update-branch until the PR merges;
# the judge -- the path the Mgo hook, /merge-flow and eng-flow run -- turns a
# GO into LEASED while a different OPEN PR holds a live one.
import time as _time

_HOME = "octo/home-repo"


def _lease_file():
    return os.environ["MERGE_LOCK_FILE"]


def _write_lease(pr=3228, repo=_HOME, expires_in=600.0, holder=None):
    with open(_lease_file(), "w", encoding="utf-8") as fh:
        json.dump({"holder": holder or "premerge_guard:resync:pr-%d:pid-1" % pr,
                   "acquired_at": _time.time(), "pid": 1, "pr": pr, "repo": repo,
                   "expires_at": _time.time() + expires_in}, fh)


def _read_lease():
    with open(_lease_file(), encoding="utf-8") as fh:
        return json.load(fh)


def _stub_lease_gates(monkeypatch, pr=3240, verdicts=("GO",), state="open"):
    """resolve() walks `verdicts` (STALE then GO for a lap); every other gate is GO."""
    calls = {"n": 0}

    def fake_resolve(*a, **k):
        v = verdicts[min(calls["n"], len(verdicts) - 1)]
        calls["n"] += 1
        return {"verdict": v, "exit_code": 3 if v == "STALE" else 0,
                "behind_by": 1 if v == "STALE" else 0, "pr": pr, "base": "main",
                "state": "OPEN", "repo": _HOME, "repo_inferred": True}

    monkeypatch.setattr(pg, "resolve", fake_resolve)
    monkeypatch.setattr(pg, "merge_time_duplicate", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0})
    monkeypatch.setattr(pg, "file_overlap_advisory", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "overlaps": []})
    monkeypatch.setattr(pg, "green_gate", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "ci": "success"})
    monkeypatch.setattr(pg, "resolve_required_check", lambda *a, **k: "agent-os tests")
    monkeypatch.setattr(pg, "_lease_applies", lambda repo: True)
    monkeypatch.setattr(pg, "lease_pr_state", lambda repo, n, gh="gh": state)


def test_judge_go_is_leased_while_another_open_pr_holds_the_lease(monkeypatch, capsys):
    _write_lease(pr=3228)
    _stub_lease_gates(monkeypatch, pr=3240)
    rc = pg.main(["--pr", "3240", "--json"])
    res = json.loads(capsys.readouterr().out)
    assert rc == pg.LEASE_HOLD == 3
    assert res["verdict"] == "LEASED" and res["stage"] == "merge"
    assert res["lease_pr"] == 3228 and res["lease_pr_state"] == "open"


def test_judge_leased_text_names_the_lease_holder(monkeypatch, capsys):
    _write_lease(pr=3228)
    _stub_lease_gates(monkeypatch, pr=3240)
    assert pg.main(["--pr", "3240"]) == 3
    out = capsys.readouterr().out
    assert out.startswith("LEASE-HOLD -- PR #3240: PR #3228 holds the merge lease"), out


def test_an_unreadable_lease_pr_still_holds_the_judge(monkeypatch, capsys):
    """No answer is not 'spent'; the lease's own expiry bounds it."""
    _write_lease(pr=3228)
    _stub_lease_gates(monkeypatch, pr=3240, state=None)
    assert pg.main(["--pr", "3240", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["lease_pr_state"] == "unknown"


def test_a_spent_lease_does_not_hold_a_merge(monkeypatch, capsys):
    _write_lease(pr=3228)
    _stub_lease_gates(monkeypatch, pr=3240, state="closed")
    assert pg.main(["--pr", "3240", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "GO"


def test_the_lease_holders_own_merge_is_not_held(monkeypatch, capsys):
    _write_lease(pr=3228)
    _stub_lease_gates(monkeypatch, pr=3228)
    assert pg.main(["--pr", "3228", "--json"]) == 0


def test_an_expired_lease_does_not_hold_a_merge(monkeypatch, capsys):
    _write_lease(pr=3228, expires_in=-1)
    _stub_lease_gates(monkeypatch, pr=3240)
    assert pg.main(["--pr", "3240", "--json"]) == 0


def test_a_lease_in_another_repo_does_not_hold_a_merge(monkeypatch, capsys):
    _write_lease(pr=3228, repo="octo/other-repo")
    _stub_lease_gates(monkeypatch, pr=3240)
    assert pg.main(["--pr", "3240", "--json"]) == 0


def test_a_plain_merge_lock_does_not_hold_the_judge(monkeypatch, capsys):
    """merge_go and eng-flow run this guard INSIDE their own plain lock."""
    with open(_lease_file(), "w", encoding="utf-8") as fh:
        json.dump({"holder": "merge_go:pr-3240:pid-9", "acquired_at": _time.time(),
                   "pid": 9}, fh)
    _stub_lease_gates(monkeypatch, pr=3240)
    assert pg.main(["--pr", "3240", "--json"]) == 0


def test_resync_takes_the_lease_before_the_lap_and_keeps_it_on_go(monkeypatch, capsys):
    _stub_lease_gates(monkeypatch, pr=3240, verdicts=("STALE", "GO"))
    seen = {}

    def fake_resync(*a, **k):
        seen["lease"] = _read_lease()
        return {"verdict": "GO", "exit_code": 0, "pr": 3240, "ci": "success"}

    monkeypatch.setattr(pg, "resync_and_wait", fake_resync)
    before = _time.time()
    rc = pg.main(["--pr", "3240", "--json", "--resync", "--resync-timeout", "1800",
                  "--lease-grace", "900"])
    res = json.loads(capsys.readouterr().out)
    assert rc == 0 and res["verdict"] == "GO"
    # taken BEFORE the update-branch, sized to cover the whole lap plus the grace
    assert seen["lease"]["pr"] == 3240 and seen["lease"]["repo"] == _HOME
    assert seen["lease"]["expires_at"] >= before + 1800 + 900
    # kept past the GO, re-sized to the grace: it now covers the wait for Mgo
    kept = _read_lease()
    assert kept["pr"] == 3240
    assert kept["expires_at"] <= _time.time() + 900
    assert res["lease"]["state"] == "kept"


def test_resync_releases_the_lease_on_a_hold(monkeypatch, capsys):
    _stub_lease_gates(monkeypatch, pr=3240, verdicts=("STALE",))
    monkeypatch.setattr(pg, "resync_and_wait", lambda *a, **k:
                        {"verdict": "BASE_MOVED", "exit_code": 3, "pr": 3240,
                         "behind_by": 1, "base": "main"})
    assert pg.main(["--pr", "3240", "--resync"]) == 3
    assert not os.path.exists(_lease_file())


def test_resync_does_not_start_a_lap_under_a_foreign_lease(monkeypatch, capsys):
    _write_lease(pr=3228)
    _stub_lease_gates(monkeypatch, pr=3240, verdicts=("STALE",))

    def must_not_run(*a, **k):
        raise AssertionError("a lap ran under PR #3228's live lease")

    monkeypatch.setattr(pg, "resync_and_wait", must_not_run)
    rc = pg.main(["--pr", "3240", "--json", "--resync"])
    res = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert res["verdict"] == "LEASED" and res["stage"] == "resync"
    assert res["lease_pr"] == 3228 and res["lease_pr_state"] == "open"
    assert _read_lease()["pr"] == 3228   # untouched


def test_resync_leased_text_says_no_lap_ran(monkeypatch, capsys):
    _write_lease(pr=3228)
    _stub_lease_gates(monkeypatch, pr=3240, verdicts=("STALE",))

    def must_not_run(*a, **k):
        raise AssertionError("a lap ran under PR #3228's live lease")

    monkeypatch.setattr(pg, "resync_and_wait", must_not_run)
    assert pg.main(["--pr", "3240", "--resync"]) == 3
    out = capsys.readouterr().out
    assert out.startswith("LEASE-HOLD -- not starting a re-sync lap for PR #3240"), out


def test_resync_breaks_a_spent_lease_and_takes_its_own(monkeypatch, capsys):
    _write_lease(pr=3228)
    _stub_lease_gates(monkeypatch, pr=3240, verdicts=("STALE", "GO"), state="closed")
    monkeypatch.setattr(pg, "resync_and_wait", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "pr": 3240, "ci": "success"})
    assert pg.main(["--pr", "3240", "--resync"]) == 0
    assert _read_lease()["pr"] == 3240


def test_resync_for_another_checkouts_repo_takes_no_lease(monkeypatch, capsys):
    """The lock file is per-checkout: a cross-repo --repo run must not lease it."""
    _stub_lease_gates(monkeypatch, pr=3240, verdicts=("STALE", "GO"))
    monkeypatch.setattr(pg, "_lease_applies", lambda repo: False)
    monkeypatch.setattr(pg, "resync_and_wait", lambda *a, **k:
                        {"verdict": "GO", "exit_code": 0, "pr": 3240, "ci": "success"})
    assert pg.main(["--pr", "3240", "--resync"]) == 0
    assert not os.path.exists(_lease_file())


def test_lease_applies_only_to_the_cwd_checkouts_own_repo(monkeypatch):
    monkeypatch.setattr(pg, "_git", lambda args, cwd=None:
                        "git@github.com:octo/home-repo.git")
    assert pg._lease_applies("octo/home-repo")
    assert pg._lease_applies("OCTO/Home-Repo")
    assert not pg._lease_applies("octo/other-repo")
    assert not pg._lease_applies(None)


def test_lease_does_not_apply_without_a_readable_origin(monkeypatch):
    def no_origin(args, cwd=None):
        raise RuntimeError("not a git repository")

    monkeypatch.setattr(pg, "_git", no_origin)
    assert not pg._lease_applies("octo/home-repo")
