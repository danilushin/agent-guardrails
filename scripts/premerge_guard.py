#!/usr/bin/env python3
"""premerge_guard.py -- pre-merge base-freshness gate (#844).

A PR whose CI went green against a STALE base can still red ``main`` when it
merges, because its checks ran *before* the base advanced. This is the
2026-06-19 #841->#842 class: #841's pre-merge CI was green, but ``main`` had
since advanced to add a durable-ledger invariant #841 broke; #841 merged anyway
and reddened ``main`` (caught only because a concurrent PR happened to re-run CI
on the freshly-merged base).

GitHub-native enforcement (merge-queue / ``require_branches_to_be_up_to_date``
branch protection) is the canonical fix, but it is plan-gated on this private
free-tier repo (``branches/main/protection`` -> HTTP 403). So the gate lives
here, in the merge path: before ``gh pr merge``, refuse to merge a PR whose
branch is behind its base -- its green is stale and must be re-validated
(re-sync the branch + let CI re-run) first.

This is the PRE-MERGE sibling of eng-flow's ``check_base_freshness``, which is a
PRE-RUN guard (don't *start* a build on a stale checkout). Different timing,
different failure mode: a run can start fresh yet go stale by merge time because
the base advances during the minutes-long build+review.

Detection (no local fetch needed): GitHub's compare API reports how many commits
the base has that the head does not -- ``behind_by``. ``behind_by == 0`` => the
branch already contains every base commit, so the green is trustworthy (GO).
``behind_by > 0`` => stale base (HOLD). This mirrors
``require_branches_to_be_up_to_date`` semantics exactly: ANY base advance can
introduce a conflicting invariant (#841 broke on a single intervening commit),
so the strictness is deliberate.

Second gate -- merge-time duplicate linkage (#844 follow-up). dup_scan's
``--prebranch`` issue->PR linkage check is sound but fires only at BRANCH time; a
parallel session that files AND merges a same-issue PR *inside* your build window
is invisible to it (the 2026-06-19 #860/#862 class: #862 was created ~5 min after
#860 branched and merged ~10 min in, so #860's branch-time scan had nothing to
find). The branch-time check has a build-window blind spot. This gate closes it
by re-running the linkage at MERGE time: if another OPEN/MERGED PR already closes
an issue this PR closes (CLOSING keywords only -- ``Closes/Fixes/Resolves #N``;
a bare ``#N`` ref or ``Part of #N`` does NOT count), HOLD -- a build-window
duplicate landed and a human must pick the winner.

Third signal -- file-overlap ADVISORY (#877). The two gates above both key on
issue->PR *linkage* (``Closes #N``), so they only see duplicates that close the
SAME issue. When two parallel sessions do duplicate/clobbering work but close
DIFFERENT (or no) issues -- the 2026-06-19 #873/#874 class -- the linkage is
blind, and only the base-freshness STALE-HOLD catches it *incidentally* (a human
reads the intervening commits). This signal surfaces it deterministically:
intersect this PR's changed files with other build-window PRs (OPEN, or MERGED
within a recency window) and print a note on any overlap. It is ADVISORY ONLY --
the exit code stays GO -- because a hard HOLD would false-positive on
legitimately-related overlapping work.

Fourth gate -- CI-green precondition (#1098). The base-freshness gate trusts the
PR's *existing* green; but eng-flow does NOT wait for CI before merging -- it
creates the PR and calls ``post_merge_discipline`` immediately, while the head
SHA's checks are still PENDING. So a red or never-run PR can merge and redden
``main`` with no stale-base involved at all. GitHub branch protection
(``required_status_checks``) is the canonical fix but is plan-gated on this
free-tier private repo (403, same wall as the base-freshness gate). This gate
makes green a hard precondition in the merge path: query the head SHA's
``statusCheckRollup`` and refuse to merge unless the required check
(``agent-os tests`` on this repo; ``resolve_required_check`` derives the right
name per-repo for a cross-repo ``--repo``, #1914 -- see its docstring)
concluded SUCCESS. Because CI is pending at eng-flow merge
time, the gate WAITS through pending up to ``--wait-green`` seconds (eng-flow
passes a generous budget); it HOLDs only on a CONFIRMED failure or an
absent/never-green check after the deadline -- ``--wait-green 0`` (the default)
blocks immediately on anything not-yet-green. A SKIPPED matching job (e.g. the
PR-only-skipped ``advance-deploy-ref`` job that shares the ``agent-os tests``
workflow name) is NOT a failure -- the workflow's green is the ``test`` job
concluding SUCCESS.

#1736 hardening -- this gate is the ONLY gate. The 2026-07-20 red-main incident
(PR #1730 merged 7 minutes AFTER its own CI concluded ``failure``) was not a
bypassed gate: branch protection CANNOT be enabled on this repo at all. It is
private on the free plan, so ``GET /repos/octo/home-repo/branches/
main/protection`` (and ``.../required_status_checks``) return HTTP 403 "Upgrade
to GitHub Pro or make this repository public to enable this feature." ``test`` is
therefore not, and cannot be, a REQUIRED status check -- nothing on GitHub's side
ever refuses a merge over red or unfinished CI. With no server-side backstop, the
CI-green gate below is load-bearing rather than advisory, which changed three
things:
  1. It names the FAILING CHECK and its run URL, so a hold is actionable in one
     step instead of sending the operator hunting.
  2. The CI-green determination now fails CLOSED (see the exit-code note below).
     "Cannot determine" is not "green", and there is no backstop to fall back to.
  3. Bypass is explicit and loud: ``--allow-red "<reason>"`` requires a real,
     non-template reason, echoes it verbatim, and records it in ``--json``. The
     old silent path (skip the gate, merge anyway) is exactly what #1736 is about.

Instrument note -- the trap that would make a naive guard worse than no guard:
on this repo ``gh run list --commit <sha>`` returns ``[]`` even for known-GREEN
commits (verified against ``f71e0a73``). A guard reading that would see a false
empty on every PR and either fail open (catastrophic) or block everything. The
instrument used here is ``gh pr view <n> --json statusCheckRollup``, proven
against live data to register BOTH states (PR #1735 -> SUCCESS, PR #1730 ->
FAILURE); ``tests/test_premerge_guard.py`` pins both verbatim rollups so the
bidirectionality is a standing assertion, not a one-off manual check.

``--resync`` -- clearing the STALE hold without a human round-trip (#2117). The
hold itself is unchanged and still right; what was missing is that CLEARING it was
entirely manual, and the class fires in BATCHES by construction (one merge to main
puts every other queued PR one commit behind, so N queued PRs cost N-1 hand-issued
``update-branch`` calls + N-1 CI re-runs -- four fires on the same two PRs in the
2026-08-14/15 session). ``--resync`` issues the server-side update-branch, waits
for the required check to conclude ON THE NEW HEAD, and re-judges.

The load-bearing part is not the API call, it is what the waiter may conclude.
``premerge_guard --pr 2109`` returned GO in that session and a concurrent session
merged ~16 minutes later, re-staling the PR before it was merged: **a GO is a
snapshot with no lease**. So (a) readiness is judged against the post-resync head
SHA, pinned into the compare (``base...<sha>``, never the branch NAME, which
GitHub re-resolves); (b) ANY base movement during the wait invalidates readiness
-- it is decided BEFORE the CI reading, because green-against-a-moved-base is
precisely the green the STALE hold refuses; and (c) ``main()`` re-resolves the
base after the wait instead of trusting the exit reading, since "ready at
watch-exit" and "ready at merge" are different moments. Exactly ONE re-sync per
invocation, deliberately: re-issuing on every base move chases a moving target and
burns a CI run per lap, and a queue is cheaper re-synced SERIALLY (two queued PRs
resynced up front cost 3 CI runs -- the first merge re-stales the second -- versus
2 done one at a time).

The re-sync lease (#3237) -- the lease that GO lacked. A lap is an update-branch
plus a CI wait on the one shared runner, ~30 min by 2026-10-22, and on that day a
sibling session's merge landed inside 2 of #3228's 4 laps (``dfcc1882`` 19 min
in, ``909e6d42`` 7 min in). Each lap ended BASE_MOVED, the lap was wasted, and
the PR went round again.
  - ``--resync`` takes a lease (``merge_lock.py``, the lock eng-flow and
    ``merge_go`` already share) BEFORE its update-branch, sized ``--resync-timeout
    + --lease-grace``.
  - On GO it keeps the lease for ``--lease-grace`` (default 30 min) to cover the
    wait for the human's ``Mgo``. On any hold it releases it.
  - The lease ends when its PR leaves OPEN, which is how the bare ``gh pr merge``
    of the Mgo path releases it, or when it lapses.
  - While a DIFFERENT OPEN PR holds a live lease, a GO from this judge becomes
    ``LEASED`` (exit 3). The Mgo hook, /merge-flow and eng-flow all run it.
  - A ``--resync`` under such a lease runs no lap: ``LEASED``, ``stage: resync``.
  - Scope: the lock file is per-checkout, so a ``--repo`` naming another repo
    takes no lease. A session on another host, or a hand-typed ``! gh pr merge``,
    is not covered.

Exit codes (0 and 3 match dup_scan.py's convention): 0 = GO (incl. a file-overlap
advisory -- it never blocks -- and an accepted ``--allow-red`` override), 3 =
HOLD (STALE base, build-window DUPLICATE, NOT-GREEN CI, or LEASED), 2 = UNDETERMINED,
5 = NOOP (the PR is not OPEN -- ``ALREADY_MERGED`` / ``ALREADY_CLOSED`` -- so
there is nothing to merge; #2748). GO means exactly one thing: open, current,
green. 4 is deliberately skipped: dup_scan.py already uses 4 for ITS
UNDETERMINED, the two tools are read side by side, and CLAUDE.md quotes that
table verbatim.

NOOP is neither a GO nor a HOLD, and each consumer already does the right thing
with it by construction: hooks/preflight_auth.py arms only on exit 0, so it does
not arm; hooks/merge_go.py refuses a non-OPEN PR before it ever runs this guard;
eng-flow.sh holds only on 3 and its ``gh pr merge`` is idempotent. Before #2748
the same state returned GO with a ``note`` that never reached the printed line,
so an already-merged PR read byte-identical to an open, ready one -- three
``Mgo`` round-trips and a one-shot marker spent on PR #2737 five hours after it
merged (2026-09-11).

Fail-open vs fail-closed is decided PER GATE, by whether a wrong answer is
recoverable:
  - Base-freshness / duplicate probes are fail-open WITHIN THIS SCRIPT (exit 2).
  - The CI-green determination FAILS CLOSED (exit 3, ``ci: "undetermined"``).
    There is no server-side required check to catch a red merge afterwards, so an
    undeterminable CI state must block. To avoid wedging on a blip, the poll loop
    RETRIES through probe failures for the whole ``--wait-green`` budget and holds
    only if it never got an answer.

That per-gate split describes THIS SCRIPT's exit codes, not the system. This
docstring used to end "Callers therefore hold the merge ONLY on exit 3, never on
any non-zero" -- false, and load-bearing: ``hooks/preflight_auth.py`` is the only
caller that arms a merge marker and it applies "non-zero means do not arm" with
no carve-out for 2, so exit 2 blocks every ``mgo``. That is correct for an
authorization hook (arming on an unreadable state is the self-arming the marker
protocol exists to prevent), so preflight_auth OWNS the arming policy and this
script does not get to describe it (#2150 Finding 1).

The old rationale for fail-open was that ``mergeable: CONFLICTING`` backstops a
stale base. It does not: STALE_HOLD fires on ``behind_by > 0``, and a branch
behind a base it does not textually conflict with merges cleanly -- which is
exactly the stale-green case #841/#842 established this gate for. Do not restore
that reasoning.

The real mitigation is a SECOND TRANSPORT, not a laxer policy: ``behind_by`` is
also measurable over git protocol, so a REST outage alone no longer blinds the
gate (see ``behind_via_git``). On 2026-08-17 the compare endpoint 404'd for ~4.5h
on private repos while git protocol worked normally, refusing a clean, green,
0-behind PR four times (#2150).

Every leg now has two transports, in this order: PR metadata -- ``gh pr view``
(GraphQL) then ``gh api repos/{nwo}/pulls/{n}`` (REST, #2785; the repo for that
path comes from ``--repo`` or the cwd's git origin, never ``gh repo view``);
freshness -- REST compare then git protocol (#2150); CI -- the GraphQL rollup
then REST check-runs (#2583). UNDETERMINED means BOTH transports of a leg failed,
and the error names both. The account's GraphQL bucket is shared by every session,
so an exhausted quota is the common way the first transport dies (2026-09-15: three
sessions merging, ``Mgo 2783`` refused on a PR that was open, clean and green).

Usage:
  premerge_guard.py --pr 844 [--repo owner/repo] [--base main] [--gh gh]
                    [--no-dup-check] [--no-overlap-check]
                    [--no-green-check] [--required-check "agent-os tests"]
                    [--wait-green 0] [--allow-red "<reason>"] [--json]
                    [--resync] [--resync-timeout 1800] [--lease-grace 1800]
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

GO = 0
STALE_HOLD = 3      # branch behind base -> do NOT merge (mirrors dup_scan PREBRANCH_HOLD)
DUP_HOLD = 3        # another PR already closes the same issue -> build-window duplicate
GREEN_HOLD = 3      # required CI check not green -> do NOT merge (#1098)
LEASE_HOLD = 3      # another OPEN PR's re-sync lap holds the merge lease (#3237)
# Both probe transports blind. FAIL-OPEN *within this script* (callers may treat
# it as advisory) but FAIL-CLOSED at the only path that arms a merge marker:
# hooks/preflight_auth.py applies "non-zero means do not arm", with no carve-out
# for 2. So in the running system exit 2 DOES block a merge; the comment that
# stood here asserted the opposite, and was false for the one path that arms
# (#2150). That is the deliberate policy -- an authorization hook must not arm on
# a state it could not read -- and preflight_auth owns it; this constant does not.
UNDETERMINED = 2
# The PR is not OPEN (already MERGED, or CLOSED without merging): nothing to
# merge, so neither a GO nor a HOLD (#2748). Distinct so no consumer can read it
# as "merge this" -- the verdict line names the merge time + sha instead. Not 4:
# dup_scan.py's 4 is UNDETERMINED and the two tables are read side by side.
NOOP = 5

# The CI-state reading when the instrument could not be read at all. Deliberately
# NOT a synonym for "absent": absent means GitHub answered and reported no such
# check; undetermined means GitHub did not answer. Both HOLD (#1736), but the
# operator's next move differs (re-run CI vs. fix connectivity/auth).
UNDETERMINED_CI = "undetermined"

# Minimum length of an --allow-red reason. Short enough not to be busywork, long
# enough that "ok"/"yes"/"fine" cannot arm an override by reflex.
OVERRIDE_MIN_REASON = 12

# The required-check name to gate on. This is normally the GitHub Actions *workflow*
# name (``.github/workflows/ci.yml`` -> ``name: ci``), which surfaces in the
# statusCheckRollup as CheckRun.workflowName -- NOT the job name (``test``). Matching
# the workflow name covers all its jobs; SKIPPED jobs are ignored (see _check_outcome).
#
# No workflow name is special in the kit. ``resolve_required_check`` takes the
# check from ``--required-check``, else the plugin's ``required_check`` option
# (exported to hook processes as REQUIRED_CHECK_ENV), else the repo's only
# workflow. Anything else is this sentinel: no real check-run carries its name,
# so the gate reads ``absent`` and HOLDs, and the name is the remedy.
REQUIRED_CHECK_ENV = "CLAUDE_PLUGIN_OPTION_REQUIRED_CHECK"
REQUIRED_CHECK_DEFAULT = ("<unresolved: pass --required-check or set the plugin's "
                          "required_check option>")

# GitHub auto-closing keywords (Closes/Fixes/Resolves + tense variants) followed by
# an issue ref. The keyword pattern is the same as dup_scan.py's close_re -- a bare
# ``#N`` ref or ``Part of #N`` is deliberately NOT a closing signal -- and both
# gates match it only AFTER _strip_code() (#2976 here; dup_scan imports this very
# function, #2981), so they agree that a keyword quoted in code is not a closer.
# Whitespace is [ \t]+, NOT \s+ -- a real GitHub closing reference is always on the
# same line as its keyword; \s+ spans blank lines, so a "## What this does NOT
# close" heading followed by a blank line and a bare "#N" in the next paragraph
# was matching as a genuine closer (#1869 false DUP-HOLD against #1864).
_CLOSE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[ \t]+#(\d+)\b", re.I)

# Code is literal text, not prose: GitHub left a keyword inside an inline code span
# unlinked (#2976 -- PR #2960's closingIssuesReferences omitted it), and a fenced
# block is code by the same rule, so a PR body that QUOTES a keyword (explaining
# why something is NOT closed) must not read as closing it. Fence: a ``` / ~~~ run
# of 3+ at <=3 spaces indent, up to a closing run of the same char at least as
# long. An UNCLOSED fence is left alone -- CommonMark runs it to end of document,
# but hiding text is the false-GO direction, counting it only HOLDs. Inline span:
# a backtick run up to the next run of exactly the same length, on the same line.
_FENCE_RE = re.compile(
    r"^[ ]{0,3}(?P<run>(?P<ch>[`~])(?P=ch){2,})[^`\n]*\n.*?"
    r"^[ ]{0,3}(?P=run)(?P=ch)*[ \t\r]*$", re.M | re.S)  # \r: CRLF bodies
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)[^\n]+?(?<!`)\1(?!`)")
# What a stripped span becomes: not whitespace (so ``Closes `x` #N`` cannot fuse
# into keyword + ref) and not a word char (so no \b boundary is erased).
_CODE_SENTINEL = "\x00"


def _strip_code(text):
    """``text`` with fenced code blocks and inline code spans replaced by a
    sentinel, so only rendered prose is scanned for closing keywords (#2976)."""
    return _CODE_SPAN_RE.sub(_CODE_SENTINEL, _FENCE_RE.sub(_CODE_SENTINEL, text))


def evaluate(behind_by):
    """Pure verdict from a behind-count. ``behind_by > 0`` => stale green => HOLD.

    Strictness is deliberate and matches require_branches_to_be_up_to_date: any
    base advance can carry a conflicting invariant, so behind_by>0 blocks.
    """
    behind_by = int(behind_by)
    if behind_by > 0:
        return {"verdict": "STALE", "exit_code": STALE_HOLD, "behind_by": behind_by}
    return {"verdict": "GO", "exit_code": GO, "behind_by": 0}


def behind_from_compare(compare):
    """Extract ``behind_by`` from a GitHub ``compare`` API object. Absent/None =>
    0 (identical or ahead-only => not behind => the green is trustworthy)."""
    if not isinstance(compare, dict):
        return 0
    return int(compare.get("behind_by") or 0)


def _closing_issues(text):
    """Issue numbers ``text`` auto-closes via GitHub keywords (Closes/Fixes/
    Resolves #N). A bare ``#N`` or ``Part of #N`` is not a closing signal, nor is
    a keyword inside a code span or fenced block (#2976)."""
    if not text:
        return set()
    return {int(n) for n in _CLOSE_RE.findall(_strip_code(text))}


def merge_time_duplicate(pr, repo=None, gh="gh", list_limit=50):
    """Does another OPEN/MERGED PR already close an issue this PR closes?

    Returns a verdict dict. The merge-time sibling of dup_scan ``--prebranch``'s
    issue->PR linkage: re-runs the same check when a build-window duplicate now
    EXISTS to be found (it did not at branch time). A non-OPEN PR, or one that
    closes no issue, is GO (nothing to gate). Only OPEN/MERGED other PRs count --
    a CLOSED-unmerged duplicate never landed."""
    repo_args = ["--repo", repo] if repo else []
    me = _gh_json(gh, ["pr", "view", str(pr), *repo_args,
                       "--json", "number,state,title,body"])
    if me.get("state") and me.get("state") != "OPEN":
        # "Nothing to CHECK" for this gate alone. main() never reaches here for a
        # non-OPEN PR -- resolve() already returned NOOP (#2748) -- so this GO is
        # only ever seen by a direct caller of this function.
        return {"verdict": "GO", "exit_code": GO, "pr": pr, "note": "PR not OPEN"}
    my_issues = _closing_issues(
        "%s\n%s" % (me.get("title") or "", me.get("body") or ""))
    if not my_issues:
        return {"verdict": "GO", "exit_code": GO, "pr": pr, "note": "closes no issue"}

    others = _gh_json(gh, ["pr", "list", "--state", "all", "--limit", str(list_limit),
                           *repo_args, "--json", "number,state,title,body,url"])
    dups = []
    for o in others:
        if int(o.get("number")) == int(pr):
            continue  # self
        if o.get("state") not in ("OPEN", "MERGED"):
            continue  # a closed-unmerged PR never landed -> not a live duplicate
        shared = my_issues & _closing_issues(
            "%s\n%s" % (o.get("title") or "", o.get("body") or ""))
        if shared:
            dups.append({"number": o.get("number"), "state": o.get("state"),
                         "title": o.get("title"), "url": o.get("url"),
                         "issues": sorted(shared)})
    if dups:
        return {"verdict": "DUPLICATE", "exit_code": DUP_HOLD, "pr": pr,
                "issues": sorted(my_issues), "duplicates": dups}
    return {"verdict": "GO", "exit_code": GO, "pr": pr, "issues": sorted(my_issues)}


def _pr_paths(pr_obj):
    """Set of changed file paths from a gh PR JSON object's ``files`` field
    (``gh pr {view,list} --json files`` -> ``[{path, additions, deletions}, ...]``)."""
    return {f.get("path") for f in (pr_obj.get("files") or []) if f.get("path")}


def _parse_iso(ts):
    """Parse a GitHub ISO-8601 ``...Z`` timestamp to an aware UTC datetime, else None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def file_overlap_advisory(pr, repo=None, gh="gh", list_limit=50,
                          window_hours=24, now=None):
    """ADVISORY (never a HOLD): does another live/build-window PR touch the same
    files as this PR?

    Closes the gap the two HOLD gates can't: ``merge_time_duplicate`` only catches
    PRs that close the *same issue*. When two parallel sessions do duplicate or
    mutually-clobbering work but close DIFFERENT (or no) issues -- the 2026-06-19
    #873/#874 class -- the linkage gate is blind, and only the base-freshness
    STALE-HOLD catches it *incidentally* (by forcing a human to read the
    intervening commits). This surfaces that signal deterministically instead.

    It is purely informational: the exit code is ALWAYS ``GO`` (0). Escalating to a
    hard HOLD would false-positive on legitimately-related overlapping work, so the
    first cut is advisory-only (per #877).

    Scope (low-noise): OPEN PRs (live concurrent work) + PRs MERGED within
    ``window_hours`` of ``now`` (a build-window landing like #873-during-#874).
    Older merged history is excluded -- every PR ever touching a shared file would
    otherwise match. ``now`` is injectable for deterministic tests."""
    repo_args = ["--repo", repo] if repo else []
    me = _gh_json(gh, ["pr", "view", str(pr), *repo_args,
                       "--json", "number,state,files"])
    if me.get("state") and me.get("state") != "OPEN":
        # Same as merge_time_duplicate: unreachable from main() for a non-OPEN
        # PR since resolve() returns NOOP first (#2748); direct callers only.
        return {"verdict": "GO", "exit_code": GO, "pr": pr, "note": "PR not OPEN",
                "advisory": False, "overlaps": []}
    my_paths = _pr_paths(me)
    if not my_paths:
        return {"verdict": "GO", "exit_code": GO, "pr": pr, "note": "no files",
                "advisory": False, "overlaps": []}

    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_hours * 3600
    others = _gh_json(gh, ["pr", "list", "--state", "all", "--limit", str(list_limit),
                           *repo_args, "--json",
                           "number,state,title,url,files,mergedAt"])
    overlaps = []
    for o in others:
        if int(o.get("number")) == int(pr):
            continue  # self
        st = o.get("state")
        if st == "MERGED":
            merged = _parse_iso(o.get("mergedAt"))
            if not merged or merged.timestamp() < cutoff:
                continue  # outside the build window -> normal history, not overlap
        elif st != "OPEN":
            continue  # a closed-unmerged PR never landed -> not live overlap
        shared = my_paths & _pr_paths(o)
        if shared:
            overlaps.append({"number": o.get("number"), "state": st,
                             "title": o.get("title"), "url": o.get("url"),
                             "files": sorted(shared)})
    return {"verdict": "GO", "exit_code": GO, "pr": pr,
            "advisory": bool(overlaps), "overlaps": overlaps}


# Candidate ``gh`` binaries, probed in order. A bare "gh" resolves only through
# the caller's PATH, and a Claude Code hook subprocess does not inherit the shell
# profile that puts the GitHub CLI there -- so every `mgo <N>` preflight raised
# [WinError 2] and returned UNDETERMINED. Fail-closed, but permanently: the
# #1779 one-step authorization path was dead in practice (#1912).
GH_EXE_CANDIDATES = (
    "gh",
    r"C:\Program Files\GitHub CLI\gh.exe",
    "/c/Program Files/GitHub CLI/gh.exe",
)

# Probing is cheap but not free, and _gh_json runs several times per invocation.
_GH_RESOLVED = None

GH_PROBE_TIMEOUT_S = 5

# The probe's argv, as a constant for the same reason ``GH_PROBE_TIMEOUT_S``
# is one: a test has to be able to drive the timeout branch with a child that
# CANNOT have exited when the deadline is checked. Behaviourally identical to
# the inline ``[cand, "--version"]`` it replaces (#2710/#2719).
GH_PROBE_ARGS = ("--version",)


def _resolve_gh(gh, candidates=GH_EXE_CANDIDATES):
    """Return a ``gh`` binary that actually answers ``--version``.

    Only the literal default ``"gh"`` is ambiguous; a path the caller named
    explicitly is a decision already made and passes through untouched -- that is
    the ``--gh`` test seam, and second-guessing it would break every injected fake.

    Probes rather than trusting the name, mirroring ``catch_up.py::_find_gh``: a
    path that *exists* is not a path that *answers*, and handing a non-responding
    binary to every downstream call just moves the failure one layer deeper.

    Raises ``RuntimeError`` naming each candidate tried. That message matters:
    ``UNDETERMINED`` already means "GitHub did not answer" (see
    ``UNDETERMINED_CI``), and "gh is not installed or not on PATH" needs a
    different operator move -- fix the environment, not the connectivity.

    Which is why a TIMEOUT gets its own branch and its own message (#2692). It
    is a THIRD operator move -- retry -- and it used to be reported as the
    second: ``TimeoutExpired`` subclasses ``SubprocessError``, so one ``except``
    swallowed "did not answer in time" and "is not there" alike, and the raise
    then asserted the one it could not distinguish. Measured quiescent, N=12,
    ``gh --version`` medians ~93ms against the 5s budget; it expires only under
    the load this gate runs in, so widening ``GH_PROBE_TIMEOUT_S`` alone would
    move the window without splitting the causes.
    """
    global _GH_RESOLVED
    if gh != "gh":
        return gh
    if _GH_RESOLVED is not None:
        return _GH_RESOLVED
    unresponsive = []
    for cand in candidates:
        try:
            probe = subprocess.run([cand, *GH_PROBE_ARGS], capture_output=True,
                                   text=True, timeout=GH_PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # STARTED but did not ANSWER -- a different fact from "absent", and
            # the only one of the two this gate can plausibly cause. Must be
            # caught BEFORE SubprocessError: TimeoutExpired is a subclass of it,
            # which is exactly how the two collapsed into one branch (#2692).
            unresponsive.append(cand)
            continue
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            _GH_RESOLVED = cand
            return cand
    if unresponsive:
        raise RuntimeError(
            "`gh` found but UNRESPONSIVE within %ss -- %s started and did not "
            "answer `--version` in time (tried %s). RETRY: the machine is loaded, "
            "not misconfigured. This is NOT a missing install and NOT a broken "
            "PATH."
            % (GH_PROBE_TIMEOUT_S, ", ".join(unresponsive), ", ".join(candidates)))
    raise RuntimeError(
        "no working `gh` binary -- tried %s. Install the GitHub CLI or put it on "
        "PATH; this is an environment gap, not a GitHub outage."
        % ", ".join(candidates))


def _gh_json(gh, args):
    """Run ``gh <args>`` and parse stdout as JSON. Raises on non-zero / bad JSON."""
    proc = subprocess.run([_resolve_gh(gh), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("gh %s failed: %s" % (" ".join(args), proc.stderr.strip()))
    return json.loads(proc.stdout)


GIT_FALLBACK_TIMEOUT_S = 60


def _git(args, cwd=None):
    """Run ``git <args>`` and return stripped stdout. Raises on non-zero."""
    proc = subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=cwd, timeout=GIT_FALLBACK_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout.strip()


def _nwo_from_url(url):
    """``owner/repo`` from an SSH or HTTPS remote URL; None if unrecognised."""
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", (url or "").strip())
    return "%s/%s" % (m.group(1), m.group(2)) if m else None


def behind_via_git(repo, base, head, cwd=None):
    """``behind_by`` measured over git protocol -- the second transport (#2150).

    Returns the count, or **None when the measurement cannot be trusted**. That
    distinction is the whole point: handing the arming path a confident wrong
    number is strictly worse than UNDETERMINED, because nothing downstream can
    tell a wrong reading from a real one. So this declines rather than guesses
    whenever it cannot prove it is measuring the right thing:

      - the local checkout's ``origin`` is not ``repo`` (``--repo`` can name a
        repo the cwd is not -- then the local graph answers about the WRONG one);
      - no origin remote at all;
      - the fetch fails (e.g. a fork head, whose ref does not exist on origin).

    Refs are re-fetched with an explicit refspec before counting, never read from
    whatever the checkout happens to have: a stale local ref would answer with
    yesterday's graph and look identical to a fresh reading.
    """
    if not (repo and base and head):
        return None
    try:
        local = _nwo_from_url(_git(["remote", "get-url", "origin"], cwd=cwd))
    except Exception:  # noqa: BLE001 - no origin / not a repo / git missing
        return None
    if not local or local.lower() != repo.lower():
        return None
    try:
        _git(["fetch", "--quiet", "origin",
              "+refs/heads/%s:refs/remotes/origin/%s" % (base, base),
              "+refs/heads/%s:refs/remotes/origin/%s" % (head, head)], cwd=cwd)
        # behind_by = commits in base not reachable from head, so head..base.
        out = _git(["rev-list", "--count",
                    "refs/remotes/origin/%s..refs/remotes/origin/%s" % (head, base)],
                   cwd=cwd)
        return int(out)
    except Exception:  # noqa: BLE001 - fetch/rev-list failure -> decline, never guess
        return None


def _pr_info_from_rest(pull):
    """The ``gh pr view --json …`` shape ``resolve`` reads, built from the REST
    ``repos/{nwo}/pulls/{n}`` payload (#2785). REST has no MERGED state: it is
    ``state: closed`` + ``merged: true``. ``merge_commit_sha`` is deliberately read
    ONLY when merged -- GitHub fills it with a test-merge sha on open PRs."""
    rest_state = (pull.get("state") or "").lower()
    if pull.get("merged"):
        state = "MERGED"
    elif rest_state == "open":
        state = "OPEN"
    else:
        state = "CLOSED"
    sha = pull.get("merge_commit_sha") if state == "MERGED" else None
    return {"number": pull.get("number"), "state": state,
            "headRefName": (pull.get("head") or {}).get("ref"),
            "baseRefName": (pull.get("base") or {}).get("ref"),
            "mergedAt": pull.get("merged_at"),
            "mergeCommit": {"oid": sha} if sha else None,
            "closedAt": pull.get("closed_at")}


def _fetch_pr_info(pr, repo, gh):
    """``(info, repo, transport)`` -- the PR's metadata over GraphQL first, REST
    when GraphQL raises (#2785, the metadata twin of #2583's rollup fallback).

    `gh pr view` is GraphQL, and the account's shared 5000/hr bucket ran dry on
    2026-09-15 with three sessions merging: the guard answered UNDETERMINED on a
    PR that was open, clean, current and green over REST -- one call BEFORE any
    of #2583's REST work could run. The REST path needs ``owner/name``, resolved by
    ``_rest_nwo`` (``--repo``, else the cwd's git origin) -- never ``gh repo view``,
    which is GraphQL and dead in exactly this situation. Raises only when BOTH
    transports fail, naming both, so the operator reading the quota error also
    learns REST was down."""
    repo_args = ["--repo", repo] if repo else []
    try:
        info = _gh_json(gh, ["pr", "view", str(pr), *repo_args,
                             "--json", "number,state,headRefName,baseRefName,"
                                       "mergedAt,mergeCommit,closedAt"])
        return info, repo, "graphql"
    except Exception as gql_exc:  # noqa: BLE001 - GraphQL blind -> REST before giving up
        nwo = _rest_nwo(repo, gh=gh)
        if not nwo:
            raise RuntimeError(
                "%s; REST fallback needs owner/repo: none passed and the cwd has no "
                "readable origin (resolving it via `gh repo view` would be GraphQL, "
                "which is the transport this fallback exists to avoid)" % gql_exc)
        try:
            pull = _gh_json(gh, ["api", "repos/%s/pulls/%s" % (nwo, pr)])
        except Exception as rest_exc:  # noqa: BLE001 - both transports blind
            raise RuntimeError("both PR-metadata transports failed -- GraphQL: %s; "
                               "REST: %s" % (gql_exc, rest_exc))
        return _pr_info_from_rest(pull), nwo, "rest"


def resolve(pr, repo=None, base=None, gh="gh"):
    """Fetch the PR's head/base, compare ``base...head``, and return the verdict
    dict enriched with pr/repo/base/head/status/transport. Raises on probe
    failure (the caller decides fail-open). A non-OPEN PR is never gated -- it is
    NOOP (``ALREADY_MERGED`` / ``ALREADY_CLOSED``, exit 5), never GO (#2748)."""
    repo_inferred = repo is None
    info, repo, transport = _fetch_pr_info(pr, repo, gh)
    state = info.get("state")
    head = info.get("headRefName")
    base = base or info.get("baseRefName") or "main"
    # #2294 -- resolve the repo BEFORE any return, so EVERY verdict names the
    # repository it actually inspected. A bare `mgo <N>` cannot express a repo
    # (hooks/preflight_auth.py PHRASE_RE is a full-match on a bare number) and the
    # hook runs this guard with cwd=PROJECT_ROOT, so `gh` infers home-repo for
    # every PR. When that number ALSO exists there, every check below runs against
    # an unrelated PR and the verdict is then read as authorization for the intended
    # one. The non-OPEN branch is the sharp edge: a wrong-repo number that happens to
    # be already merged short-circuits immediately (before #2748 that was a GO, and
    # preflight_auth armed on its exit 0). Naming the repo (and whether it was
    # guessed) makes that visible in the guard's own output instead of only in an
    # error string.
    if not repo:
        # Still on GraphQL when GraphQL just answered `pr view` (unchanged, and the
        # hermetic tests stub it); on the REST leg `_fetch_pr_info` already named
        # the repo from git origin, so this is never reached there (#2785). If
        # THIS call is the one the quota kills, fall back the same way.
        try:
            repo = _gh_json(gh, ["repo", "view", "--json", "nameWithOwner"]).get("nameWithOwner")
        except Exception as gql_exc:  # noqa: BLE001 - GraphQL blind -> git origin
            repo = _rest_nwo(None, gh=gh)
            if not repo:
                raise RuntimeError(
                    "%s; and the cwd has no readable origin to name the repo from" % gql_exc)
    if state and state != "OPEN":
        # Already merged / closed -> nothing to gate, and nothing to MERGE. Not GO:
        # every consumer upstream of the human reads GO as "merge this", and the
        # old `note` never reached the printed line (#2748). Carry the merge
        # time + sha so the verdict can say WHEN it landed, not just that it did.
        merge_commit = info.get("mergeCommit")
        sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        return {"verdict": "ALREADY_MERGED" if state == "MERGED" else "ALREADY_CLOSED",
                "exit_code": NOOP, "behind_by": 0,
                "pr": pr, "repo": repo, "repo_inferred": repo_inferred,
                "base": base, "state": state, "transport": transport,
                "merged_at": info.get("mergedAt"), "merge_commit": sha,
                "closed_at": info.get("closedAt"),
                "note": "PR not OPEN -- nothing to merge"}
    try:
        compare = _gh_json(gh, ["api", "repos/%s/compare/%s...%s" % (repo, base, head)])
        behind, status, probe = behind_from_compare(compare), compare.get("status"), "rest"
    except Exception:  # noqa: BLE001 - REST blind -> try git protocol before giving up
        behind = behind_via_git(repo, base, head)
        if behind is None:
            raise          # both transports blind: UNDETERMINED, never a silent GO
        status, probe = None, "git"
    res = evaluate(behind)
    res.update({"pr": pr, "repo": repo, "repo_inferred": repo_inferred,
                "base": base, "head": head,
                "status": status, "state": state, "probe": probe,
                "transport": transport})
    return res


def _match_check(check, required):
    """Does a statusCheckRollup entry belong to the ``required`` check?

    Our CI is a GitHub Actions workflow named ``agent-os tests`` -- its CheckRun
    entries carry that string in ``workflowName`` (the job ``name`` is ``test`` /
    ``advance-deploy-ref``). Legacy StatusContext entries use ``context``. Match
    case-insensitively against workflowName / name / context, exact-or-substring,
    so ``--required-check "agent-os tests"`` catches every job of that workflow."""
    req = (required or "").strip().lower()
    if not req:
        return False
    for field in ("workflowName", "name", "context"):
        val = check.get(field)
        if val and (req == str(val).strip().lower() or req in str(val).strip().lower()):
            return True
    return False


def _check_outcome(check):
    """Reduce one matching check entry to 'success' | 'pending' | 'failure' |
    'skipped'.

    CheckRun: not-COMPLETED status (QUEUED/IN_PROGRESS) => pending; conclusion
    SUCCESS => success; SKIPPED/NEUTRAL => skipped (a skipped job is NOT a failure
    -- the PR-only ``advance-deploy-ref`` job is conditionally skipped and must be
    ignored); anything else (FAILURE/CANCELLED/TIMED_OUT/ACTION_REQUIRED/...) =>
    failure. StatusContext: state PENDING/EXPECTED => pending; SUCCESS => success;
    else failure."""
    typename = check.get("__typename")
    if typename == "StatusContext" or (check.get("state") and not check.get("status")):
        state = str(check.get("state") or "").upper()
        if state in ("PENDING", "EXPECTED"):
            return "pending"
        return "success" if state == "SUCCESS" else "failure"
    # CheckRun (default)
    status = str(check.get("status") or "").upper()
    if status and status != "COMPLETED":
        return "pending"
    conclusion = str(check.get("conclusion") or "").upper()
    if conclusion == "SUCCESS":
        return "success"
    if conclusion in ("SKIPPED", "NEUTRAL"):
        return "skipped"
    if not conclusion and not status:
        return "pending"  # no signal yet
    return "failure"


def evaluate_checks(rollup, required):
    """Pure verdict over a statusCheckRollup list for the required check:
    'success' | 'pending' | 'failure' | 'absent'.

    - No matching entry => 'absent' (the required check has not been reported).
    - Any matching FAILURE => 'failure' (one red job fails the workflow).
    - Else any matching PENDING => 'pending' (still running).
    - Else any matching SUCCESS => 'success' (skipped-only siblings ignored).
    - Only-skipped matches => 'absent' (nothing meaningful ran)."""
    matches = [c for c in (rollup or []) if _match_check(c, required)]
    if not matches:
        return "absent"
    outcomes = [_check_outcome(c) for c in matches]
    if "failure" in outcomes:
        return "failure"
    if "pending" in outcomes:
        return "pending"
    if "success" in outcomes:
        return "success"
    return "absent"  # all matches were skipped -> nothing actually ran


def _check_url(check):
    """The human-visitable URL for one rollup entry. CheckRun carries ``detailsUrl``
    (the Actions job page); legacy StatusContext carries ``targetUrl``."""
    for field in ("detailsUrl", "targetUrl", "url"):
        val = check.get(field)
        if val:
            return str(val)
    return ""


def _check_label(check):
    """Best human name for one rollup entry: the JOB name (``test``) is what an
    operator needs, falling back to the status context or workflow name."""
    for field in ("name", "context", "workflowName"):
        val = check.get(field)
        if val:
            return str(val)
    return "?"


def failing_checks(rollup, required):
    """Every matching check that is NOT green, as ``[{name, outcome, url}]``.

    Requirement (2) of #1736: a hold must be actionable in one step. Without the
    job name and its run URL, "CI is not green" sends the operator hunting through
    the Actions tab -- and a guard that is annoying to act on is a guard that gets
    bypassed. SKIPPED/NEUTRAL siblings are excluded for the same reason
    ``_check_outcome`` treats them as non-failures: the PR-only ``advance-deploy-ref``
    job is always skipped and naming it would be pure noise."""
    out = []
    for check in (rollup or []):
        if not _match_check(check, required):
            continue
        outcome = _check_outcome(check)
        if outcome in ("success", "skipped"):
            continue
        out.append({"name": _check_label(check), "outcome": outcome,
                    "url": _check_url(check)})
    return out


def fetch_check_rollup(pr, repo=None, gh="gh"):
    """The head SHA's statusCheckRollup list for ``pr`` (may raise -> fail-open).

    GraphQL (measured: graphql=1, rest=0). ``fetch_rollup_any`` wraps this with
    the #2583 REST fallback; call that, not this, unless you specifically want
    the GraphQL reading."""
    repo_args = ["--repo", repo] if repo else []
    info = _gh_json(gh, ["pr", "view", str(pr), *repo_args,
                         "--json", "statusCheckRollup"])
    return info.get("statusCheckRollup") or []


# ---- #2583: a REST reading of the same thing, for an exhausted GraphQL bucket -
#
# `gh pr view` is GraphQL, and the account's 5000/hr GraphQL budget is shared by
# every concurrent session (measured 2026-09-05, ref/workflow.md "Which `gh`
# commands spend the GraphQL quota"). When it runs dry the gate goes blind and
# fails closed on PRs that are green, 0-behind and mergeable -- three `Mgo`
# refusals on PR #2575 that day, cleared only by merging by hand.
#
# THE TRAP, and why this is a join rather than a rename: the REST check-runs
# payload has NO `workflowName` -- only JOB names (`test`, `advance-deploy-ref`,
# `labels`). `_match_check` gates on the WORKFLOW name ("agent-os tests"), so an
# adapter that just maps `name` matches nothing, returns `absent`, and HOLDs
# exactly as before: a fallback that looks like a fix and changes nothing. The
# workflow name is recoverable from a SECOND REST endpoint,
# `/actions/runs?head_sha=`, joined to each check-run by the Actions run id that
# `_run_id` already parses out of `details_url`.
#
# `head_sha` MUST be the full 40-char SHA. Measured 2026-09-05: the 8-char form
# returns `total_count: 0` with no error, which would silently empty the join map
# and re-open the trap above. (Same footgun as `gh run list --commit`, whose
# "dead instrument" reputation in this repo turns out to be exactly this -- it
# answers correctly when given 40 chars.)

def _rest_nwo(repo, gh="gh"):
    """``owner/name`` for the REST paths, WITHOUT spending GraphQL.

    ``repo`` when given. Otherwise the cwd's origin, read over plain git -- never
    `gh repo view`, which is GraphQL and therefore unavailable in exactly the
    situation this fallback exists for."""
    if repo:
        return repo
    try:
        return _nwo_from_url(_git(["remote", "get-url", "origin"]))
    except Exception:  # noqa: BLE001 - not a repo / no origin -> caller raises
        return None


def _workflow_names_for_sha(nwo, head_sha, gh="gh"):
    """``{actions run id (str): workflow name}`` for ``head_sha``, or ``{}``.

    Best-effort by design: a failure here costs the workflow-name match (job-name
    matching still works, e.g. `--required-check test`) but must not collapse the
    whole REST fallback into a raise."""
    try:
        payload = _gh_json(gh, ["api", "repos/%s/actions/runs?head_sha=%s"
                                % (nwo, head_sha)])
    except Exception:  # noqa: BLE001 - degrade to job names, never crash
        return {}
    out = {}
    for run in (payload or {}).get("workflow_runs") or []:
        rid, name = run.get("id"), run.get("name")
        if rid is not None and name:
            out[str(rid)] = str(name)
    return out


def _latest_run_per_name(check_runs):
    """One entry per check name: the highest ``id``, i.e. the newest run.

    `/commits/{sha}/check-runs` is SHA-addressed and keeps SUPERSEDED runs. On PR
    #2579's head f998952b it returned `labels` CANCELLED (101298030067) beside
    `labels` SUCCESS (101298085307); read together, `_check_outcome` scores the
    stale cancel as a failure and the gate HOLDs a green PR. List order is not a
    recency proxy, so this keys on the id."""
    best = {}
    for run in check_runs or []:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "?")
        try:
            rid = int(run.get("id") or 0)
        except (TypeError, ValueError):
            rid = 0
        if name not in best or rid > best[name][0]:
            best[name] = (rid, run)
    return [run for _, run in best.values()]


def _adapt_rest_check_run(run, workflow_names):
    """One REST check-run in the shape `_match_check` / `_check_outcome` read.

    `_check_outcome` upper-cases `status` / `conclusion` itself, so REST's
    lowercase values pass through untouched."""
    return {
        "__typename": "CheckRun",
        "name": run.get("name"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "workflowName": workflow_names.get(_run_id(run) or ""),
        "detailsUrl": run.get("details_url") or run.get("html_url") or "",
    }


def fetch_rollup_via_rest(pr, repo=None, gh="gh"):
    """A statusCheckRollup-shaped list for ``pr``, over REST only (#2583).

    Three REST calls: the PR (for its full head SHA), that SHA's check-runs, and
    the workflow-name join. Raises if the first two fail -- the caller decides,
    and `green_gate` still fails CLOSED when both transports are blind."""
    nwo = _rest_nwo(repo, gh=gh)
    if not nwo:
        raise RuntimeError(
            "REST fallback needs owner/repo: none passed and the cwd has no "
            "readable origin (resolving it via `gh repo view` would be GraphQL, "
            "which is the transport this fallback exists to avoid)")
    pull = _gh_json(gh, ["api", "repos/%s/pulls/%s" % (nwo, pr)])
    head_sha = str(((pull or {}).get("head") or {}).get("sha") or "")
    if len(head_sha) < 40:
        raise RuntimeError(
            "REST fallback got no usable head SHA for %s#%s (got %r); the "
            "workflow-name join needs the full 40-char form -- an abbreviated "
            "one returns zero rows with no error" % (nwo, pr, head_sha))
    return _rest_rollup_for_sha(nwo, head_sha, gh=gh)


def _rest_rollup_for_sha(nwo, head_sha, gh="gh"):
    """The rollup-shaped check list for ``head_sha`` over REST. SHA-addressed, so
    it cannot carry another commit's runs (#2232) -- already bound to the head."""
    payload = _gh_json(gh, ["api", "repos/%s/commits/%s/check-runs"
                            % (nwo, head_sha)])
    runs = _latest_run_per_name((payload or {}).get("check_runs") or [])
    workflow_names = _workflow_names_for_sha(nwo, head_sha, gh=gh)
    return [_adapt_rest_check_run(r, workflow_names) for r in runs]


def fetch_rollup_any(pr, repo=None, gh="gh"):
    """``(rollup, transport)`` -- GraphQL first, REST fallback (#2583).

    GraphQL stays primary: it carries `workflowName` natively and costs one
    request. REST is tried only when GraphQL raises, and raises itself only when
    BOTH are blind -- carrying both error strings, so an operator who fixes the
    quota also learns REST was down."""
    try:
        return fetch_check_rollup(pr, repo=repo, gh=gh), "graphql"
    except Exception as graphql_exc:  # noqa: BLE001 - try the other transport
        try:
            return fetch_rollup_via_rest(pr, repo=repo, gh=gh), "rest"
        except Exception as rest_exc:  # noqa: BLE001 - both blind -> fail closed
            raise RuntimeError(
                "both CI transports failed -- graphql: %s | rest: %s"
                % (graphql_exc, rest_exc))


def resolve_required_check(repo, gh="gh", required_check=None):
    """Resolve the CI check name to gate on for ``repo`` (#1914).

    A hardcoded default check name meant every invocation against a repo whose
    CI is named differently demanded a check that repo could never produce,
    HOLDing forever with no recoverable next step. So the name is resolved:

    - An explicit ``required_check`` (``--required-check`` passed on the CLI)
      always wins verbatim -- this never overrides an operator's explicit choice.
    - Else the plugin's ``required_check`` option, read from REQUIRED_CHECK_ENV.
      Claude Code exports it to hook processes only, not to commands run through
      the Bash tool, so from Bash ``--required-check`` is the way in. A blank
      value counts as unset.
    - No ``repo`` at all -> the strict sentinel. Callers must hand in the repo
      they actually inspected (``_resolved_repo`` in ``main()``), never a bare
      ``args.repo``: a hook runs this guard without ``--repo`` from the
      SESSION's cwd (#2294), so ``None`` here says nothing about the repo (#2743).
    - Exactly one workflow on ``repo`` -> that workflow's name.
    - Ambiguous (zero or >1 workflow) or the workflow probe itself fails -> the
      sentinel. Deliberately the STRICT fallback, not a permissive one: no
      rollup ever contains it -> ``absent`` -> HOLD. A wrong-but-strict derive
      costs an operator one ``--required-check``; a wrong-and-permissive one
      would silently wave a merge through."""
    if required_check is not None:
        return required_check
    configured = (os.environ.get(REQUIRED_CHECK_ENV) or "").strip()
    if configured:
        return configured
    if not repo:
        return REQUIRED_CHECK_DEFAULT
    try:
        workflows = _gh_json(gh, ["api", "repos/%s/actions/workflows" % repo])
        names = [w.get("name") for w in (workflows.get("workflows") or []) if w.get("name")]
    except Exception:  # noqa: BLE001 - probe failure -> strict fallback, not weak
        return REQUIRED_CHECK_DEFAULT
    if len(names) == 1:
        return names[0]
    return REQUIRED_CHECK_DEFAULT


def green_gate(pr, repo=None, gh="gh", required=REQUIRED_CHECK_DEFAULT,
               wait_green=0, poll_interval=20.0,
               sleep=time.sleep, now=time.monotonic):
    """Wait for the required CI check to conclude, up to ``wait_green`` seconds.

    Returns a verdict dict. GO when SUCCESS; GREEN_HOLD (exit 3) on a confirmed
    FAILURE (immediately) or on pending/absent that never resolves within the
    deadline. eng-flow merges while CI is PENDING, so the wait is mandatory for the
    --auto-merge path -- a bare assert would HOLD every run. ``wait_green=0`` blocks
    immediately on anything not-yet-green (the standalone-tool default). ``sleep``
    / ``now`` are injectable so tests never actually wait.

    Probe failures FAIL CLOSED (#1736). This function no longer propagates a
    ``fetch_check_rollup`` exception for the caller to swallow: with branch
    protection 403-gated on this repo there is no server-side required check to
    catch a red merge afterwards, so an unreadable instrument must block, not wave
    through. To keep that from wedging a genuinely-green PR on one blip, a failed
    poll is RETRIED for the rest of the ``wait_green`` budget; only a budget that
    expires with no answer at all returns ``ci = "undetermined"`` + GREEN_HOLD,
    carrying the last probe error so the operator can fix the right thing."""
    deadline = now() + max(0.0, wait_green)
    last_error = None
    transport = None
    while True:
        try:
            # #2583: GraphQL first, REST fallback. An exhausted GraphQL bucket is
            # a fact about the quota, not about this PR's CI.
            rollup, transport = fetch_rollup_any(pr, repo=repo, gh=gh)
            verdict = evaluate_checks(rollup, required)
            failing = failing_checks(rollup, required)
            last_error = None
        except Exception as exc:  # noqa: BLE001 - BOTH transports blind -> fail CLOSED
            verdict, failing, transport = UNDETERMINED_CI, [], None
            last_error = str(exc)
        if verdict == "success":
            return {"verdict": "GO", "exit_code": GO, "pr": pr,
                    "check": required, "ci": "success", "failing": [],
                    "transport": transport}
        if verdict == "failure":
            return {"verdict": "NOT_GREEN", "exit_code": GREEN_HOLD, "pr": pr,
                    "check": required, "ci": "failure", "failing": failing,
                    "transport": transport}
        # pending / absent / undetermined -> keep polling until the deadline, then HOLD
        if now() >= deadline:
            res = {"verdict": "NOT_GREEN", "exit_code": GREEN_HOLD, "pr": pr,
                   "check": required, "ci": verdict, "failing": failing,
                   "transport": transport}
            if last_error:
                res["error"] = last_error
            return res
        sleep(min(poll_interval, max(0.1, deadline - now())))


# ---- #2117: clearing a STALE hold without a human round-trip ----------------
# The STALE hold above is correct and unchanged. What was missing is that CLEARING
# it was entirely manual, and the class fires in BATCHES by construction: one merge
# to main puts every other queued PR one commit behind, so N queued PRs cost N-1
# hand-issued `update-branch` calls plus N-1 CI re-runs. Observed 2026-08-14/15:
# four fires on the same two PRs (#2109 + #2112) in a single session.
#
# The subtle half is not issuing the API call -- it is what the waiter is allowed
# to conclude afterwards. Fire 4 of that session: the guard returned
# "GO -- up to date with main", and a concurrent session merged ~16 minutes later,
# staling the PR again before the merge command ran. A GO is a SNAPSHOT WITH NO
# LEASE. So the waiter below judges readiness against the post-resync head SHA and
# treats any base movement during the wait as invalidating readiness, and main()
# re-resolves the base after the wait rather than trusting the exit reading.

# How long to wait for CI to conclude on the freshly-resynced head. Sized off the
# observed ~7-minute CI runs plus queueing slack; the resync itself lands in
# seconds. A wait that expires is a HOLD, never a pass.
RESYNC_TIMEOUT_DEFAULT = 1800.0


def request_update_branch(pr, repo, gh="gh"):
    """Ask GitHub to merge ``base`` into the PR's head branch -- the server-side,
    no-checkout, no-force-push re-sync the STALE message has named since #2024.

    Raises on refusal (HTTP 409 for a genuine merge conflict is the common one;
    HTTP 422 when the head moved under us, e.g. a sibling's re-sync). The caller
    re-probes once: still behind => a standing hold, since a refused re-sync
    cleared nothing and reporting anything softer would claim work that did not
    happen; no longer behind => someone else cleared it, so it waits for CI."""
    return _gh_json(gh, ["api", "--method", "PUT",
                         "repos/%s/pulls/%s/update-branch" % (repo, pr)])


_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")


def _run_id(check):
    """The Actions run id a check entry belongs to, or ``None``.

    Both shapes carry it in a URL: the GraphQL rollup as ``detailsUrl``, the REST
    check-runs payload as ``details_url``. ``None`` means "not an Actions run"
    (a legacy StatusContext) OR "unparseable" -- callers must treat those the
    same way, as unjudgeable rather than as mismatched."""
    for field in ("detailsUrl", "details_url", "url"):
        val = check.get(field)
        if val:
            match = _RUN_ID_RE.search(str(val))
            if match:
                return match.group(1)
    return None


def fetch_head_run_ids(repo, head_sha, gh="gh"):
    """The Actions run ids GitHub attributes to ``head_sha`` itself.

    ``repos/{repo}/commits/{sha}/check-runs`` is SHA-ADDRESSED: it cannot return
    another commit's runs, which is precisely the property the PR-level rollup
    lacks. Raises on failure -- the caller is inside a retry budget, and a probe
    that cannot bind must not silently fall back to the unbound reading (#2232)."""
    payload = _gh_json(gh, ["api", "repos/%s/commits/%s/check-runs"
                            % (repo, head_sha)])
    runs = (payload or {}).get("check_runs") or []
    return {rid for rid in (_run_id(run) for run in runs) if rid}


def bind_rollup_to_head(rollup, head_run_ids):
    """Drop rollup entries whose Actions run is NOT attached to the probed SHA.

    #2232: for a window after ``update-branch``, ``gh pr view`` pairs the NEW
    ``headRefOid`` with the PREVIOUS head's concluded runs. Reading them together
    binds them syntactically -- one HTTP response -- but not semantically, and a
    concluded SUCCESS belonging to another commit is not evidence about this one.

    Entries with no run id (legacy StatusContext) are KEPT: the intersection has
    nothing to say about them, and dropping the unjudgeable would silently mute a
    whole class of required check rather than wait for it."""
    kept = []
    for check in rollup or []:
        run_id = _run_id(check)
        if run_id is None or run_id in head_run_ids:
            kept.append(check)
    return kept


def probe_merge_readiness(pr, repo=None, base=None, gh="gh",
                          required=REQUIRED_CHECK_DEFAULT):
    """One BOUND reading of merge readiness: the head SHA, the checks reported for
    that SHA, and how far behind ``base`` that same SHA is.

    Binding is the point, and it takes TWO steps because one response is not one
    reading:

    1. The compare pins the SHA verbatim (``base...<sha>``) instead of the branch
       NAME -- a name is re-resolved server-side, so a push landing between the two
       calls would yield a behind-count describing a different commit than the green
       about to be trusted. Same class of mismatch the STALE hold exists for, one
       layer down.
    2. The rollup arrives in the same response as ``headRefOid`` but is NOT thereby
       bound to it. GitHub serves ``statusCheckRollup`` denormalized, and for a
       window after ``update-branch`` it pairs the new SHA with the PREVIOUS head's
       concluded runs (#2232 -- that inherited SUCCESS returned a GO in ~0s of an
       1800s budget). So the rollup is intersected with the runs the SHA-addressed
       ``commits/{sha}/check-runs`` endpoint attributes to this exact commit.

    Step 1 guards a stale BASE; step 2 guards a stale GREEN. Neither implies the
    other, and the single-response shape supplies neither on its own.

    Two transports (#2817, the probe twin of #2785/#2583): GraphQL ``pr view``
    first; when it raises, REST -- ``pulls/{n}`` for the head SHA, then that SHA's
    ``check-runs``, which are SHA-addressed and so already bound (step 2 is free
    there). Without this leg ``--resync`` died on its first probe under a GraphQL
    limit, the very outage that piles up stale bases (2026-09-23, PR #2814)."""
    repo_args = ["--repo", repo] if repo else []
    try:
        info = _gh_json(gh, ["pr", "view", str(pr), *repo_args, "--json",
                             "number,state,headRefName,headRefOid,baseRefName,"
                             "statusCheckRollup"])
        transport = "graphql"
    except Exception as gql_exc:  # noqa: BLE001 - GraphQL blind -> REST before giving up
        nwo = _rest_nwo(repo, gh=gh)
        if not nwo:
            raise RuntimeError(
                "%s; REST fallback needs owner/repo: none passed and the cwd has no "
                "readable origin" % gql_exc)
        try:
            pull = _gh_json(gh, ["api", "repos/%s/pulls/%s" % (nwo, pr)])
            info = dict(_pr_info_from_rest(pull),
                        headRefOid=((pull or {}).get("head") or {}).get("sha"))
            if not info["headRefOid"]:
                raise RuntimeError("pulls/%s carried no head SHA" % pr)
            info["statusCheckRollup"] = _rest_rollup_for_sha(nwo, info["headRefOid"], gh=gh)
        except Exception as rest_exc:  # noqa: BLE001 - both transports blind
            raise RuntimeError("both readiness transports failed -- GraphQL: %s; "
                               "REST: %s" % (gql_exc, rest_exc))
        repo, transport = nwo, "rest"
    base = base or info.get("baseRefName") or "main"
    head_sha = info.get("headRefOid")
    rollup = info.get("statusCheckRollup") or []
    if not repo:
        try:
            repo = _gh_json(gh, ["repo", "view", "--json",
                                 "nameWithOwner"]).get("nameWithOwner")
        except Exception as gql_exc:  # noqa: BLE001 - GraphQL blind -> git origin
            repo = _rest_nwo(None, gh=gh)
            if not repo:
                raise RuntimeError(
                    "%s; and the cwd has no readable origin to name the repo from" % gql_exc)
    if transport == "graphql":
        # The rollup arrived in the same response as head_sha, but GitHub serves it
        # denormalized -- see bind_rollup_to_head. Intersect it with the runs the
        # SHA-addressed endpoint attributes to this exact commit before judging.
        rollup = bind_rollup_to_head(rollup, fetch_head_run_ids(repo, head_sha, gh=gh))
    compare = _gh_json(gh, ["api", "repos/%s/compare/%s...%s" % (repo, base, head_sha)])
    return {"pr": pr, "repo": repo, "base": base, "state": info.get("state"),
            "head": info.get("headRefName"), "head_sha": head_sha,
            "behind_by": behind_from_compare(compare),
            "check": required, "ci": evaluate_checks(rollup, required),
            "failing": failing_checks(rollup, required), "transport": transport}


def evaluate_resync(probe, head_before):
    """Pure verdict for one bound readiness probe taken after a re-sync request.
    'GO' | 'WAITING' | 'BASE_MOVED' | 'NOT_GREEN'.

    The ORDER of these tests is the contract, not a style choice:

    1. Head SHA unchanged (``head_before``) => WAITING. The update-branch merge
       commit has not landed, so the branch is still the stale object and its
       checks still describe the stale base. ``head_before=None`` skips this test
       (nothing was re-synced -- plain wait-for-green).
    2. ``behind_by > 0`` => BASE_MOVED HOLD, decided BEFORE CI is consulted. A
       green against a base that has since advanced is exactly the green the STALE
       hold refuses; letting ``ci == "success"`` answer first would re-create fire
       4 of the 2026-08-14/15 session with an automation's confidence behind it.
    3. Then CI on the new head: failure => hold; pending/absent => keep waiting;
       success => GO.
    """
    head_sha = probe.get("head_sha")
    behind = int(probe.get("behind_by") or 0)
    ci = probe.get("ci")
    common = {"pr": probe.get("pr"), "repo": probe.get("repo"),
              "base": probe.get("base"), "head_sha": head_sha,
              "behind_by": behind, "check": probe.get("check"), "ci": ci}
    if head_before is not None and head_sha == head_before:
        return dict(common, verdict="WAITING", exit_code=STALE_HOLD,
                    note="re-sync has not landed yet (head unmoved)")
    if behind > 0:
        return dict(common, verdict="BASE_MOVED", exit_code=STALE_HOLD)
    if ci == "failure":
        return dict(common, verdict="NOT_GREEN", exit_code=GREEN_HOLD,
                    failing=probe.get("failing") or [])
    if ci == "success":
        return dict(common, verdict="GO", exit_code=GO)
    return dict(common, verdict="WAITING", exit_code=GREEN_HOLD,
                note="checks have not concluded on the new head")


def resync_and_wait(pr, repo=None, base=None, gh="gh",
                    required=REQUIRED_CHECK_DEFAULT,
                    timeout=RESYNC_TIMEOUT_DEFAULT, poll_interval=20.0,
                    sleep=time.sleep, now=time.monotonic):
    """Clear a STALE hold end-to-end: request the server-side re-sync, then wait
    for the required check to conclude ON THE NEW HEAD, re-judging every poll.

    Returns a verdict dict; only ``GO`` (exit 0) means ready. Every other exit is
    3 -- ``BASE_MOVED`` (a concurrent merge re-staled it mid-wait), ``NOT_GREEN``
    (the fresh base exposed a real failure -- the hold working as designed, not a
    setback), ``RESYNC_FAILED`` (GitHub refused the update and a re-probe still
    reads the PR behind, e.g. a real merge conflict, or the re-probe failed),
    ``TIMEOUT`` (the budget expired with nothing concluded).

    A refusal is re-read ONCE before it is believed (#3208): a sibling session
    that re-synced the PR between our probe and our PUT draws HTTP 422, and a
    re-probe reading ``behind_by == 0`` turns that into the plain CI wait on the
    current head (``concurrent_resync`` + a note on the verdict), never a hold.

    Deliberately ONE re-sync per invocation. A waiter that re-issued update-branch
    on every base move would chase a moving target on a busy repo and burn a CI run
    per lap; the queue economics also say resync SERIALLY (with two queued PRs,
    resyncing both up front costs 3 CI runs because the first merge re-stales the
    second, versus 2 done one at a time). So a mid-wait base move ends the wait
    and the operator re-runs -- one lap, one CI run, an honest verdict.

    Probe failures do not decide anything: they are retried for the rest of the
    budget and only an expired budget holds, carrying the last error. Same
    fail-CLOSED contract as ``green_gate``, for the same reason -- there is no
    server-side gate behind this one. ``sleep``/``now`` are injectable so tests
    never actually wait."""
    try:
        before = probe_merge_readiness(pr, repo=repo, base=base, gh=gh,
                                       required=required)
    except Exception as exc:  # noqa: BLE001 - cannot re-sync what we cannot read
        return {"verdict": "RESYNC_FAILED", "exit_code": STALE_HOLD, "pr": pr,
                "repo": repo, "base": base, "error": str(exc), "stage": "probe",
                "note": "readiness probe failed before the re-sync was requested"}

    repo = before.get("repo") or repo
    base = before.get("base") or base
    issued = False
    concurrent = None
    if int(before.get("behind_by") or 0) > 0:
        try:
            request_update_branch(pr, repo, gh=gh)
            issued = True
        except Exception as exc:  # noqa: BLE001 - a refused re-sync cleared nothing
            refused = {"verdict": "RESYNC_FAILED", "exit_code": STALE_HOLD, "pr": pr,
                       "repo": repo, "base": base, "error": str(exc),
                       "stage": "update_branch"}
            # #3208: the refusal says only that OUR request did not land. A
            # sibling that re-synced between our probe and our PUT draws HTTP 422
            # ("expected head sha didn't match"), and that PR needs no re-sync --
            # so re-read once and let behind_by decide. Never re-issue the PUT.
            try:
                after = probe_merge_readiness(pr, repo=repo, base=base, gh=gh,
                                              required=required)
            except Exception as probe_exc:  # noqa: BLE001 - undecidable -> hold
                refused["reprobe_error"] = str(probe_exc)
                return refused
            if int(after.get("behind_by") or 0) > 0:
                return refused
            concurrent = {"concurrent_resync": True, "update_branch_error": str(exc),
                          "note": "update-branch was refused but the PR is no "
                                  "longer behind: a concurrent re-sync was "
                                  "detected, so CI was waited on the current head"}
    # Only a re-sync we actually requested licenses the "wait for the head to
    # move" test; without one there is no new SHA coming and every poll would
    # WAIT forever on an unmoving head. A concurrent re-sync is the same case:
    # its new SHA has already landed.
    head_before = before.get("head_sha") if issued else None

    deadline = now() + max(0.0, timeout)
    probe, last_error = None, None
    while True:
        try:
            probe = probe_merge_readiness(pr, repo=repo, base=base, gh=gh,
                                          required=required)
            last_error = None
        except Exception as exc:  # noqa: BLE001 - a blip decides nothing; retry
            probe, last_error = None, str(exc)
        if probe is not None:
            res = evaluate_resync(probe, head_before)
            if res["verdict"] != "WAITING":
                res["resync_issued"] = issued
                res.update(concurrent or {})
                return res
        if now() >= deadline:
            res = {"verdict": "TIMEOUT", "exit_code": STALE_HOLD, "pr": pr,
                   "repo": repo, "base": base, "resync_issued": issued,
                   "timeout": timeout}
            if probe is not None:
                res.update({"head_sha": probe.get("head_sha"),
                            "behind_by": probe.get("behind_by"),
                            "ci": probe.get("ci"), "check": probe.get("check"),
                            "failing": probe.get("failing") or []})
            if last_error:
                res["error"] = last_error
            res.update(concurrent or {})
            return res
        sleep(min(poll_interval, max(0.1, deadline - now())))


LEASE_GRACE_DEFAULT = 1800.0
_MERGE_LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merge_lock.py")


def _load_merge_lock():
    """merge_lock.py by path, or None. A missing lock degrades to no lease: a
    serializer must never wedge the fleet harder than no lock (its own contract)."""
    try:
        spec = importlib.util.spec_from_file_location("merge_lock", _MERGE_LOCK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 - degrade to unleased, never block
        return None


def _lease_lock_file(ml):
    """The lock eng-flow and merge_go already contend on; MERGE_LOCK_FILE is its CLI's override."""
    return os.environ.get("MERGE_LOCK_FILE") or ml.default_lock_path()


def _lease_applies(repo):
    """The lock file is the cwd checkout's, so it leases only that checkout's own PRs."""
    try:
        local = _nwo_from_url(_git(["remote", "get-url", "origin"]))
    except Exception:  # noqa: BLE001 - no origin / not a repo -> no lease
        return False
    return bool(repo) and bool(local) and local.lower() == repo.lower()


def lease_pr_state(repo, pr, gh="gh"):
    """'open' / 'closed' for the PR a lease protects (REST), or None when unreadable."""
    try:
        return (_gh_json(gh, ["api", "repos/%s/pulls/%d" % (repo, pr)]).get("state")
                or None)
    except Exception:  # noqa: BLE001 - unreadable -> the lease counts as live
        return None


def _leased(pr, base, info, stage):
    return {"verdict": "LEASED", "exit_code": LEASE_HOLD, "pr": pr, "base": base,
            "stage": stage, "lease_pr": info.get("pr"),
            "lease_holder": info.get("holder"),
            "lease_expires_in": info.get("expires_in"),
            "lease_pr_state": info.get("pr_state") or "unknown"}


def foreign_lease(pr, repo, base=None, gh="gh"):
    """A LEASED verdict while a different OPEN PR of ``repo`` holds a live lease (#3237).

    None for a free lock, an expired or spent lease, this PR's own lease, another
    repo's, and a plain lock with no ``pr`` -- merge_go and eng-flow run this guard
    INSIDE their own plain lock, so it must never hold them."""
    ml = _load_merge_lock()
    if ml is None or not repo:
        return None
    st = ml.status(_lease_lock_file(ml))
    lease_pr = st.get("pr")
    if st.get("state") != "held" or lease_pr is None or lease_pr == pr:
        return None
    lease_repo = st.get("repo") or repo
    if lease_repo.lower() != repo.lower():
        return None
    state = lease_pr_state(lease_repo, lease_pr, gh=gh)
    if state is not None and state != "open":
        return None
    return _leased(pr, base, dict(st, pr_state=state), "merge")


def take_lease(pr, repo, expires_in, gh="gh"):
    """Take the merge lease before a re-sync lap (#3237).

    Returns {'state': 'acquired' | 'held' | 'skipped' | 'error', ...}; on 'held'
    it carries the holder's status. 'skipped' / 'error' run the lap unleased."""
    if not _lease_applies(repo):
        return {"state": "skipped"}
    ml = _load_merge_lock()
    if ml is None:
        return {"state": "error"}
    lock_file = _lease_lock_file(ml)
    holder = "premerge_guard:resync:pr-%d:pid-%d" % (pr, os.getpid())
    probed = {}

    def _probe(r, n):
        probed["pr_state"] = lease_pr_state(r, n, gh=gh)
        return probed["pr_state"]

    try:
        got = ml.acquire(lock_file, holder, wait=0, pr=pr, repo=repo,
                         expires_in=expires_in, pr_state=_probe)
    except Exception:  # noqa: BLE001 - fail-open: the lap runs unleased
        got = "error"
    lease = {"state": got, "lock_file": lock_file, "holder": holder}
    if got == "held":
        lease["info"] = dict(ml.status(lock_file), pr_state=probed.get("pr_state"))
    return lease


def finish_lease(lease, go, pr, repo, grace, gh="gh"):
    """After the lap: on GO keep the lease for ``grace`` seconds -- the wait for
    the human's Mgo, where a sibling merge would re-stale it again -- else release
    it, since an abandoned lap protects nothing. Returns what happened, or None."""
    if not lease or lease.get("state") != "acquired":
        return None
    ml = _load_merge_lock()
    if ml is None:
        return None
    try:
        if go:
            got = ml.acquire(lease["lock_file"], lease["holder"], wait=0, pr=pr,
                             repo=repo, expires_in=grace,
                             pr_state=lambda r, n: lease_pr_state(r, n, gh=gh))
            return {"state": "kept" if got == "acquired" else got, "expires_in": grace}
        ml.release(lease["lock_file"], holder=lease["holder"])
        return {"state": "released"}
    except Exception:  # noqa: BLE001 - expires_at still bounds a lease we could not touch
        return {"state": "error"}


def validate_override_reason(reason):
    """``(ok, why_not)`` for an ``--allow-red`` reason.

    Bypass must be possible (a red self-hosted runner is a real, legitimate case)
    but never convenient or reflexive -- requirement (3) of #1736. Three ways to
    fail: empty, an unedited ``<placeholder>`` pasted straight out of the help text
    (the same class authorize_merge.py rejects), or a token too short to be a
    reason at all."""
    r = (reason or "").strip()
    if not r:
        return False, "the reason is empty"
    if "<" in r and ">" in r:
        return False, ("the reason is still the unedited <placeholder> -- replace it "
                       "with the actual justification")
    if len(r) < OVERRIDE_MIN_REASON:
        return False, ("the reason is %d chars; at least %d are required -- state why "
                       "merging red is safe" % (len(r), OVERRIDE_MIN_REASON))
    return True, ""


def apply_allow_red(res, reason):
    """Convert a NOT_GREEN hold into an audited ALLOW_RED pass, or leave it held.

    Scoped to the CI-green hold ONLY: a STALE base or a build-window DUPLICATE is a
    different failure class that ``--allow-red`` says nothing about, and widening
    one flag to release every gate is how a targeted override becomes a blanket
    one. Returns ``(res, banner_lines)``; ``banner_lines`` is empty when nothing
    was overridden."""
    if res.get("verdict") != "NOT_GREEN" or reason is None:
        return res, []
    ok, why_not = validate_override_reason(reason)
    if not ok:
        return res, ["ALLOW-RED REFUSED -- %s. The hold stands." % why_not]
    clean = reason.strip()
    passed = {"verdict": "ALLOW_RED", "exit_code": GO, "pr": res.get("pr"),
              "check": res.get("check"), "ci": res.get("ci"),
              "override_reason": clean, "overridden": res}
    banner = [
        "!" * 72,
        "ALLOW-RED OVERRIDE -- merging PR #%s with CI *not* green (%s)."
        % (res.get("pr"), res.get("ci")),
        "Reason given: %s" % clean,
        "This repo has NO server-side gate (branch protection is Pro-gated, 403),",
        "so this override is the only thing standing between a red check and main.",
        "If the reason above is not true, stop and fix CI instead.",
        "!" * 72,
    ]
    return passed, banner


def _format_failing(failing):
    """Indented '- <job> (<outcome>) -- <url>' lines for a hold message."""
    lines = []
    for f in failing or []:
        url = (" -- %s" % f["url"]) if f.get("url") else ""
        lines.append("    - %s (%s)%s" % (f.get("name"), f.get("outcome"), url))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pre-merge base-freshness gate (#844).")
    ap.add_argument("--pr", type=int, required=True, help="PR number to gate")
    ap.add_argument("--repo", default=None, help="owner/repo (default: gh-inferred)")
    ap.add_argument("--base", default=None, help="base branch (default: PR's baseRefName)")
    ap.add_argument("--gh", default="gh", help="gh binary (test seam)")
    ap.add_argument("--no-dup-check", action="store_true",
                    help="skip the merge-time duplicate-linkage check")
    ap.add_argument("--no-overlap-check", action="store_true",
                    help="skip the file-overlap advisory (#877)")
    ap.add_argument("--no-green-check", action="store_true",
                    help="skip the CI-green precondition (#1098)")
    ap.add_argument("--required-check", default=None,
                    help="required CI check name (default: the plugin's "
                         "required_check option from $%s, else the repo's only "
                         "workflow; anything else HOLDs, #1914)"
                         % REQUIRED_CHECK_ENV)
    ap.add_argument("--wait-green", type=float, default=0.0,
                    help="seconds to wait through pending CI before HOLDing (#1098)")
    ap.add_argument("--resync", action="store_true",
                    help="on a STALE hold, request the server-side update-branch "
                         "and wait for CI to conclude on the NEW head, then "
                         "re-judge (#2117). Opt-in: this is the only flag that "
                         "makes the gate WRITE to the PR.")
    ap.add_argument("--resync-timeout", type=float, default=RESYNC_TIMEOUT_DEFAULT,
                    help="seconds to wait for the re-synced head to go green "
                         "(default %d); an expired budget HOLDs (#2117)"
                         % RESYNC_TIMEOUT_DEFAULT)
    ap.add_argument("--lease-grace", type=float, default=LEASE_GRACE_DEFAULT,
                    help="seconds a --resync lap's merge lease outlives its GO, "
                         "covering the wait for Mgo (default %d, #3237)"
                         % LEASE_GRACE_DEFAULT)
    ap.add_argument("--allow-red", default=None, metavar="REASON",
                    help="override the CI-green HOLD with an explicit reason (#1736). "
                         "Loud and audited; does NOT release a stale-base or "
                         "duplicate hold. Reason must be real, not a <placeholder>.")
    ap.add_argument("--json", action="store_true", help="machine-readable verdict")
    args = ap.parse_args(argv)

    try:
        res = resolve(args.pr, repo=args.repo, base=args.base, gh=args.gh)
    except Exception as exc:  # noqa: BLE001 - both transports blind
        # Reached only when REST *and* git protocol are both unusable (resolve()
        # tries behind_via_git before re-raising). Exit 2 is fail-open here but
        # fail-CLOSED at hooks/preflight_auth.py, the only marker-arming caller,
        # which is deliberate and owns that policy -- see the module docstring.
        # Do not "restore" the old fail-open note: it claimed
        # mergeable:CONFLICTING backstops a stale base, and it does not (#2150).
        if args.json:
            # #2294: a pre-resolve failure still reports WHICH repo was asked and
            # whether it was guessed -- the false-HOLD branch (a valid cross-repo
            # PR probed against the wrong repo) is otherwise diagnosable only by
            # reading the gh error string.
            print(json.dumps({"verdict": "UNDETERMINED", "exit_code": UNDETERMINED,
                              "repo": args.repo, "repo_inferred": args.repo is None,
                              "error": str(exc)}))
        else:
            print("UNDETERMINED -- BOTH freshness transports failed. REST: %s. "
                  "git protocol could not answer either (wrong/absent origin, or "
                  "the refs would not fetch). Whether this BLOCKS depends on the "
                  "caller: one that merges only on exit 0 (the `mgo <N>` phrase "
                  "arm) is blocked, but one that reads exit 2 as this guard's own "
                  "fail-open verdict PROCEEDS TO MERGE. To clear: fix whichever "
                  "transport is down -- "
                  "`gh api repos/<owner/repo>/compare/<base>...<head>` and "
                  "`git fetch origin` are the two probes." % exc, file=sys.stderr)
        return UNDETERMINED

    # #2117: opt-in automated clearing of the STALE hold. Runs the server-side
    # re-sync + waits for CI on the NEW head. On its GO the base is RE-RESOLVED
    # rather than trusting the waiter's exit reading: those are two different
    # moments, and the gap between them is where fire 4 of the 2026-08-14/15
    # session happened (a verified GO, re-staled 16 min later by a concurrent
    # merge). A hold from the waiter IS the verdict and replaces the STALE one.
    # #2294 -- captured from the base resolution before any later gate can
    # replace `res`, so the emitted verdict always names the inspected repo.
    _resolved_repo = res.get("repo") or args.repo
    _resolved_repo_inferred = res.get("repo_inferred", args.repo is None)

    resync_res = None
    lease = None
    if res["verdict"] == "STALE" and args.resync:
        # #3237 -- the lease is taken BEFORE the update-branch, so no sibling
        # merge lands inside the lap; a live one held by another OPEN PR means
        # no lap at all, since its merge would only re-stale this one.
        lease = take_lease(args.pr, _resolved_repo,
                           args.resync_timeout + args.lease_grace, gh=args.gh)
    if lease is not None and lease["state"] == "held":
        res = _leased(args.pr, res.get("base"), lease.get("info") or {}, "resync")
    elif res["verdict"] == "STALE" and args.resync:
        # #2743 -- derive from the repo actually inspected, not args.repo: the
        # hook invokes this guard bare (cwd = session repo, no --repo, #2294),
        # so args.repo is None while `gh` inferred a foreign repo. Handing the
        # resolver None returned the home constant, and every bare `mgo <N>`
        # from an other-repo cwd HOLD a green PR on `agent-os tests: absent`.
        required_check = resolve_required_check(_resolved_repo, gh=args.gh,
                                                required_check=args.required_check)
        try:
            resync_res = resync_and_wait(
                args.pr, repo=res.get("repo") or args.repo, base=res.get("base"),
                gh=args.gh, required=required_check, timeout=args.resync_timeout)
        except Exception as exc:  # noqa: BLE001 - a crashed waiter cleared nothing
            resync_res = {"verdict": "RESYNC_FAILED", "exit_code": STALE_HOLD,
                          "pr": args.pr, "base": res.get("base"),
                          "error": str(exc)}
        if resync_res["exit_code"] != GO:
            res = resync_res
        else:
            try:
                res = resolve(args.pr, repo=args.repo, base=args.base, gh=args.gh)
            except Exception as exc:  # noqa: BLE001 - same both-transports-blind path
                finish_lease(lease, False, args.pr, _resolved_repo, args.lease_grace,
                             gh=args.gh)
                if args.json:
                    print(json.dumps({"verdict": "UNDETERMINED",
                                      "exit_code": UNDETERMINED, "error": str(exc),
                                      "resync": resync_res}))
                else:
                    print("UNDETERMINED -- post-re-sync freshness probe failed on "
                          "BOTH transports (%s). Whether this BLOCKS depends on "
                          "the caller: one that merges only on exit 0 (the "
                          "`mgo <N>` phrase arm) is blocked, but one that reads "
                          "exit 2 as this guard's own fail-open verdict PROCEEDS "
                          "TO MERGE. The re-sync above already ran."
                          % exc, file=sys.stderr)
                return UNDETERMINED

    # #3237 -- a GO is a snapshot with no lease; this is the lease's other half.
    # While a different OPEN PR's lap holds it (from its update-branch until it
    # merges or the lease lapses), merging here would re-stale that lap. The Mgo
    # hook, /merge-flow and eng-flow all run this judge before `gh pr merge`.
    if res["verdict"] == "GO":
        try:
            held = foreign_lease(args.pr, _resolved_repo, base=res.get("base"),
                                 gh=args.gh)
        except Exception:  # noqa: BLE001 - fail-open, merge_lock's own contract
            held = None
        if held:
            res = held

    # Second gate: merge-time duplicate linkage. Only meaningful when the base is
    # fresh (a STALE hold already blocks). A dup-probe failure is fail-open -- it
    # must never block a merge, so swallow and keep the base GO.
    if res["verdict"] == "GO" and not args.no_dup_check:
        try:
            dup = merge_time_duplicate(args.pr, repo=args.repo, gh=args.gh)
            if dup["verdict"] == "DUPLICATE":
                res = dup
        except Exception:  # noqa: BLE001 - dup-probe failure is fail-open
            pass

    # Fourth gate: CI-green precondition (#1098). Only on the GO path (a stale/dup
    # hold already blocks). eng-flow merges while CI is PENDING, so green_gate waits
    # through pending up to --wait-green before HOLDing; it HOLDs only on confirmed
    # red or never-green-by-deadline. A green-probe failure is fail-open -- it must
    # never block a merge, so swallow and keep the prior verdict.
    if res["verdict"] == "GO" and not args.no_green_check:
        # #2743 -- `_resolved_repo`, not args.repo (see the resync site above).
        required_check = resolve_required_check(_resolved_repo, gh=args.gh,
                                                 required_check=args.required_check)
        try:
            green = green_gate(args.pr, repo=args.repo, gh=args.gh,
                               required=required_check, wait_green=args.wait_green)
        except Exception as exc:  # noqa: BLE001 - unreadable instrument -> fail CLOSED
            # #1736 reversal: this used to swallow the exception and keep the GO.
            # With branch protection 403-gated there is no server-side required
            # check behind us, so a CI state we could not read must HOLD -- an
            # undetermined reading is evidence about the instrument, not about CI.
            green = {"verdict": "NOT_GREEN", "exit_code": GREEN_HOLD, "pr": args.pr,
                     "check": required_check, "ci": UNDETERMINED_CI,
                     "failing": [], "error": str(exc)}
        if green["exit_code"] != GO:
            res = green

    # Explicit, loud, reasoned bypass of the CI-green hold only (#1736).
    res, allow_red_banner = apply_allow_red(res, args.allow_red)
    for line in allow_red_banner:
        print(line, file=sys.stderr)

    # Third signal: file-overlap ADVISORY (#877). Only on the GO path -- a held PR
    # needs no advice. NEVER changes the exit code. Fail-open: a probe failure is
    # silent (the advisory is a hint, not a dependency).
    overlap = None
    if res["verdict"] == "GO" and not args.no_overlap_check:
        try:
            ov = file_overlap_advisory(args.pr, repo=args.repo, gh=args.gh)
            if ov.get("overlaps"):
                overlap = ov
        except Exception:  # noqa: BLE001 - advisory probe failure is fail-open
            pass

    lease_end = finish_lease(lease, res["exit_code"] == GO, args.pr, _resolved_repo,
                             args.lease_grace, gh=args.gh)

    if args.json:
        if overlap:
            res["file_overlap"] = overlap["overlaps"]
        if resync_res is not None:
            res["resync"] = resync_res
        if lease_end is not None:
            res["lease"] = lease_end
        # #2294: the dup/green/overlap gates return their OWN verdict dicts and
        # replace `res` wholesale, so carry the resolved repo across. setdefault,
        # never assignment -- a gate that already named a repo keeps its own.
        res.setdefault("repo", _resolved_repo)
        res.setdefault("repo_inferred", _resolved_repo_inferred)
        print(json.dumps(res))
    elif res["verdict"] == "LEASED":
        who = ("PR #%s" % res["lease_pr"]) if res.get("lease_pr") is not None \
            else "a merge in flight"
        left = res.get("lease_expires_in")
        held_by = "holder %s, %s" % (
            res.get("lease_holder"),
            "expires in %d min" % max(1, round(left / 60)) if left is not None
            else "a plain merge lock, TTL-bounded")
        if res.get("stage") == "resync":
            print("LEASE-HOLD -- not starting a re-sync lap for PR #%s: %s holds the "
                  "merge lease (%s). A lap now would only be re-staled when it "
                  "merges (#3237). Re-run --resync after it merges or the lease "
                  "lapses." % (res["pr"], who, held_by))
        else:
            print("LEASE-HOLD -- PR #%s: %s holds the merge lease (%s) -- its re-sync "
                  "lap is running or waiting on its Mgo, and merging #%s now would "
                  "re-stale it (#3237). Merge it first, or re-send after it merges "
                  "or the lease lapses." % (res["pr"], who, held_by, res["pr"]))
        if res.get("lease_holder"):
            print("  If that PR is abandoned, release it by hand: merge_lock.py "
                  "release --holder %s" % res["lease_holder"])
    elif res["verdict"] == "BASE_MOVED":
        print("BASE-MOVED HOLD -- PR #%s was re-synced and then fell %d commit(s) "
              "behind %s again while its CI ran: a concurrent merge landed during "
              "the wait. Its green is once more a green against a base that has "
              "moved, which is the state this gate refuses -- readiness is "
              "INVALIDATED, not merely stale-again."
              % (res.get("pr"), res.get("behind_by"), res.get("base")))
        print("  Re-run with --resync to take another lap (one re-sync + one CI "
              "run per lap, deliberately; chasing a moving base in-loop burns a "
              "CI run per concurrent merge).")
    elif res["verdict"] == "RESYNC_FAILED" and res.get("stage") == "probe":
        # #2817: the probe died BEFORE any request -- saying "GitHub refused" and
        # pointing at a 409 sent the operator after a conflict that did not exist.
        print("RESYNC-FAILED HOLD -- PR #%s: could not read the PR's merge readiness, "
              "so the update-branch request was never sent; the stale-base hold "
              "stands." % res.get("pr"))
        if res.get("error"):
            print("  %s" % res["error"])
        print("  The one-call clear still works by hand once a transport answers:")
        print("    gh api --method PUT repos/%s/pulls/%s/update-branch"
              % (res.get("repo") or "<owner>/<name>", res.get("pr")))
    elif res["verdict"] == "RESYNC_FAILED":
        print("RESYNC-FAILED HOLD -- PR #%s: GitHub refused the update-branch "
              "request, so nothing was cleared and the stale-base hold stands."
              % res.get("pr"))
        if res.get("error"):
            print("  %s" % res["error"])
        print("  A 409 here is a genuine merge conflict: resolve it on the branch "
              "(the one case the server-side one-call re-sync cannot do for you).")
    elif res["verdict"] == "TIMEOUT":
        print("RESYNC-TIMEOUT HOLD -- PR #%s: waited %.0fs after the re-sync and "
              "nothing concluded (last reading: ci=%s, behind_by=%s). An expired "
              "budget is a HOLD, never a pass."
              % (res.get("pr"), res.get("timeout") or 0, res.get("ci"),
                 res.get("behind_by")))
        if res.get("error"):
            print("  Last probe error: %s" % res["error"])
            print("  Failing CLOSED: an unreadable instrument is not evidence of "
                  "green.")
    elif res["verdict"] == "STALE":
        # Surface-to-clear (#2024). The old text said "re-sync the branch (merge
        # main + push)" -- a LOCAL recovery describing a checkout the person
        # merging usually does not have, so it cost a multi-minute round-trip
        # whose only purpose was to move a ref. GitHub does the whole thing
        # server-side in one call. This class fires in BATCHES by construction:
        # one merge to main puts every other queued PR one commit behind, so N
        # queued merges HOLD N-1 of them and the unnamed command is paid N-1
        # times. `repo` is set by resolve() on every path that can reach STALE;
        # args.repo is the belt-and-braces fallback for a stubbed/refactored
        # result, because `repos/None/pulls/...` would look copy-pasteable and
        # fail on paste -- worse than saying nothing.
        nwo = res.get("repo") or args.repo
        print("STALE-HOLD -- PR #%s is %d commit(s) behind %s. Its green CI ran "
              "against a stale base (the #841->#842 class)."
              % (res["pr"], res["behind_by"], res["base"]))
        if nwo:
            # #2117: name the one-command clear FIRST. The manual pair below is
            # still printed -- it is what --resync automates, and an operator who
            # only ever sees the automated form cannot debug it when it holds.
            print("  Clear it in one command (re-sync + wait for CI on the NEW "
                  "head + re-judge):")
            print("    %s --pr %s%s --resync"
                  % (sys.argv[0], res["pr"],
                     (" --repo %s" % nwo) if args.repo else ""))
            print("  Or by hand, server-side -- one call, no checkout, no "
                  "force-push:")
            print("    gh api --method PUT repos/%s/pulls/%s/update-branch"
                  % (nwo, res["pr"]))
            print("  Then wait for CI to re-run ON THE NEW BASE, and merge. The "
                  "re-sync does NOT release this hold -- green on the new base is "
                  "the whole point of it.")
        else:
            print("  Re-sync the branch (merge %s + push), let CI re-run, then "
                  "merge." % res["base"])
    elif res["verdict"] == "DUPLICATE":
        d = res["duplicates"][0]
        print("DUP-HOLD -- PR #%s closes issue(s) %s already being closed by "
              "PR #%s (%s). A build-window duplicate landed (the #860/#862 class); "
              "do NOT merge -- pick which PR wins and close the other.\n  %s"
              % (res["pr"], res["issues"], d["number"], d["state"], d.get("url") or ""))
    elif res["verdict"] == "NOT_GREEN":
        ci = res.get("ci")
        why = ("the required check FAILED" if ci == "failure"
               else "the required check has not run" if ci == "absent"
               else "the CI state could NOT be determined" if ci == UNDETERMINED_CI
               else "CI is still pending")
        print("GREEN-HOLD -- PR #%s: %s (%s) for required check '%s'. There is NO "
              "server-side gate on this repo -- branch protection is Pro-gated "
              "(HTTP 403), so '%s' is not a REQUIRED check and nothing on GitHub's "
              "side will refuse this merge (the #1730 class: merged 7 min after its "
              "own CI concluded failure). Do NOT merge until '%s' concludes SUCCESS "
              "on the head SHA; if it failed, fix + push + re-run CI first."
              % (res["pr"], why, ci, res.get("check"), res.get("check"),
                 res.get("check")))
        failing_lines = _format_failing(res.get("failing"))
        if failing_lines:
            print("  Blocking check(s):")
            for line in failing_lines:
                print(line)
        if res.get("error"):
            print("  Probe error: %s" % res["error"])
            print("  Failing CLOSED: an unreadable instrument is not evidence of "
                  "green, and there is no server-side check behind this one.")
        print("  Override (loud + audited, CI-green hold only): "
              "--allow-red \"<why merging red is safe>\"")
    elif res["verdict"] == "ALLOW_RED":
        print("ALLOW-RED -- PR #%s merged past a NOT-GREEN CI state (%s) on an "
              "explicit reason: %s" % (res.get("pr"), res.get("ci"),
                                       res.get("override_reason")))
    elif res["verdict"] == "ALREADY_MERGED":
        # #2748: must not START with "GO" -- that token is what the `Mgo` chain
        # (preflight_auth -> marker -> agent) reads as "merge this". Name WHEN
        # it landed so a stale `Mgo` costs one line, not a marker or a resync.
        sha = res.get("merge_commit")
        print("ALREADY MERGED -- PR #%s merged at %s (%s); nothing to merge."
              % (res["pr"], res.get("merged_at") or "unknown time",
                 sha[:8] if sha else "sha unknown"))
    elif res["verdict"] == "ALREADY_CLOSED":
        print("ALREADY CLOSED -- PR #%s was closed without merging at %s; nothing "
              "to merge. Reopen it first if it should land."
              % (res["pr"], res.get("closed_at") or "unknown time"))
    else:
        print("GO -- PR #%s is up to date with %s; CI green is trustworthy."
              % (res["pr"], res["base"]))
        if lease_end and lease_end.get("state") == "kept":
            print("LEASE -- PR #%s keeps the merge lease for %d min, so a sibling "
                  "session's merge waits for this PR's Mgo instead of re-staling it "
                  "(#3237)." % (res["pr"], round(lease_end["expires_in"] / 60)))
        if overlap:
            lines = "\n".join(
                "  PR #%s [%s] %s -- %s" % (
                    o["number"], o["state"], o.get("url") or "",
                    ", ".join(o["files"]))
                for o in overlap["overlaps"])
            print("ADVISORY -- PR #%s shares changed file(s) with %d build-window "
                  "PR(s); possible duplicate/clobbering work that closes a different "
                  "(or no) issue, which the linkage gate can't see (the #873/#874 "
                  "class). NOT a HOLD -- review before merge:\n%s"
                  % (res["pr"], len(overlap["overlaps"]), lines))
    return res["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
