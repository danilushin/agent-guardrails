#!/usr/bin/env python3
"""dup_scan.py -- remote-duplicate advisory for the eng-flow preflight (#622).

Given the GH issue a run is about to build, flag any OPEN or recently-MERGED PR
that already closes/fixes/resolves it -- i.e. a parallel session (or an earlier
run) already did this work. This lets eng-flow WARN before spending tokens or
creating a branch, instead of discovering the collision only at merge via
`mergeable: CONFLICTING` (the #612 / #618 duplicate cycles, 2026-06-15).

Scope split (see memory `feedback-gogogo-conflict-check`, item 6): eng-flow can
only know the *issue* pre-branch -- target FILES emerge during the execute phase
-- so this scans issue->PR linkage. The agent's go/gogogo conflict pre-check
owns the file-overlap half (it knows its target files from the task brief).

Contract:
  - stdin: none. Shells out to `<gh> pr list` for open + recently-merged PRs.
  - stdout: one advisory line per matching PR -- ``#<n> <title> [<state>] <url>``.
    Empty output == no duplicate found.
  - exit: ALWAYS 0. Advisory only; fail-open on any gh/network/parse error so an
    offline run never blocks (mirrors check_base_freshness' fail-open stance).

GH_BIN seam: ``--gh <path>`` (default ``gh``) so tests inject a fake gh, matching
reconcile_prefetch.py / pr_sweep_prefetch.py.

``--prebranch`` mode (#687): the solo-inline ``gogogo`` path has no enforced
preflight -- it scans at turn-start, then builds for minutes, so a concurrent
session can file+merge a duplicate in the build window (the 2026-06-16 #659/#678
collisions). This mode bundles the three immediately-before-branching checks into
one callable the inline path runs as its LAST step before ``git worktree add`` /
first edit: (1) ``git fetch`` (refresh the stale turn-start view; ``--no-fetch``
skips it for tests), (2) issue-state re-verify (CLOSED ⇒ already resolved),
(3) issue→PR linkage AND file-overlap vs the target set (``--files``).
Any hit prints ``HOLD`` + reasons and exits ``3``; a clean preflight prints ``GO``
and exits ``0``. Infra failure fails OPEN (exit 0) like the default advisory mode
-- the merge-time ``mergeable: CONFLICTING`` check stays the last-resort backstop.
One infra failure is carved OUT of that fail-open (#2507): when the target repo
cannot be resolved at all, the remote probe never ran, so the verdict is
``UNDETERMINED`` and exits ``4`` rather than reporting a GO it did not earn.
Precedence is HOLD (3) > UNDETERMINED (4) > GO (0) -- a positive finding outranks
a blind scan. Deliberately NARROW: a total ``gh`` outage leaves the repo equally
unresolvable but keeps the old fail-open GO, because revoking fail-open is a wider
contract change than #2507's scope (the two are told apart by whether the issue-state
query answered). That residual is real and tracked, not overlooked. eng-flow.sh
reads exit 4 as UNDETERMINED at pre-PR (#2533): it warns and opens the PR anyway,
the same advisory contract as a HOLD there. Its pre-branch scan runs the advisory
mode (no gate flag), which always exits 0.

GraphQL-only limit (#2827): `gh issue view` / `gh pr list` / `gh repo view` are all
GraphQL, and a GraphQL limit used to fail the issue-state query too -- so it read
as "gh is down" and skipped the CLOSED-issue HOLD while REST was alive. The
issue-state query now retries over REST (``gh api repos/<nwo>/issues/<n>``, the
repo from the qualifier or the cwd's git origin), so "answered" means EITHER
transport answered and a CLOSED issue HOLDs under the limit.

#2831 finished the job for the other two GraphQL legs. The PR listings retry over
REST (``/pulls``; open PRs also fetch ``/pulls/<n>/files``, merged PRs do not --
their file overlap is advisory-only, and a stderr note says it went unchecked),
and the cwd repo falls back from `gh repo view` to the git origin, so a qualified
SAME-repo scan is not mistaken for cross-repo and still matches a bare
``Closes #N``. Under a GraphQL-only limit the scan therefore reaches a real
verdict, bare or qualified. When a listing is unreadable on BOTH transports while
the issue state answered, the linkage/file-overlap probe never ran, so the verdict
is UNDETERMINED (4), never GO -- the #2507 rule applied one leg over. A total
outage still keeps the fail-open GO.

``--pre-pr`` mode (#2013): ``--prebranch`` moved the scan from turn-start to
immediately-before-branching, which closed turn-start->branch and left
branch->PR wide open -- the LARGER window, since that is the whole build. On
2026-08-09 a sibling session opened PR #2009 on issue #2006 **33s** after this
session's worktree was created; both prebranch scans were clean and correct, and
nothing asked again, so PR #2010 was opened 10m31s later against an already-open
duplicate. Detection was never the problem: at PR-open time #2009 was OPEN with
``Closes #2006`` in its body, so re-running the checks above *unchanged* would
have HELD. This mode is that second ask. Two things differ from ``--prebranch``:
the advice (the work already EXISTS, so "do not branch" is useless -- the live
options are file-as-competing / rebase-to-delta / discard / harvest), and the
file set (``--files`` is DERIVED from ``git diff --name-only <base>...HEAD`` when
omitted -- at PR time the target files are ground truth, not the guess the
scope-split note above describes). ``--since <iso>`` labels each hit *build-window*
(created at/after the branch point: this class, the tooling working as designed)
or *pre-existing* (a genuine ``--prebranch`` miss) -- without it a retro cannot
tell the two apart. Same exit contract (3 = HOLD, 0 = GO), but callers should
treat a HOLD here as ADVISORY: aborting a finished build strands the work, and the
point is that a human sees the collision while both branches still exist.

Empty derived target set (#3047, ``--pre-pr`` only): ``<base>...HEAD`` sees
COMMITS only and ``_diff_files`` is fail-open, so uncommitted work and a git
failure both derive an empty set -- and the open-PR file-overlap leg, the one this
mode exists for, then had nothing to intersect while the scan printed nothing and
exited GO. It now prints ``note — target set EMPTY (...); open-PR file-overlap NOT
checked`` and the verdict is UNDETERMINED (4), never GO: run it after committing,
or pass ``--files``. Same precedence (a HOLD from another leg still exits 3) and
same ``gh``-alive gate as the #2507/#2831 cases, so a total outage keeps the
fail-open GO (the note still prints).

Base-freshness advisory (#2219, ``--pre-pr`` only): the same window that lets a
sibling file a duplicate also lets the BASE move. That was checked at merge time
and nowhere earlier -- premerge_guard STALE-HOLDs any ``behind_by > 0`` -- so a PR
filed off a moved base was undetectable until ``mgo``, after it had already burnt
a CI run against the wrong base. This mode now also measures ``behind_by`` over
THREE transports and prints a one-line advisory naming the count and the fix:
the compare API, then the git protocol (that pair is #2150's -- it exists because
the compare endpoint 404'd for ~4.5h and reported 0-behind four times), then a
purely local count. The local one is not redundant: both remote transports
resolve the head ref on origin, and at ``--pre-pr`` time the branch usually is
not pushed yet, so without it the advisory would go UNDETERMINED on most of the
runs it exists for. It NEVER changes the exit code, and a base no transport can
measure prints a loud UNDETERMINED note rather than the silence a fresh base
gets. ``--no-base-check`` suppresses the probe.

Local-overlap advisory (#1070): the three checks above are all *remote* -- they
see pushed PRs and the issue's GH state, but are blind to a concurrent session's
**unpushed** local worktree or branch already building the same issue (the gap
that let ``build-1055`` build #1055 invisibly while a second session also took it).
``--prebranch`` therefore also scans local git: any worktree whose checked-out
branch slug or HEAD commit references the target issue, and any local branch whose
slug encodes it (excluding the current branch -- self-match). This is a **loud
advisory, never a HOLD**: a legitimate second worktree on a related-but-different
branch must not be blocked, so local overlap prints under ``LOCAL OVERLAP`` and
leaves the exit code untouched (only the remote checks decide HOLD vs GO).
``--no-local`` skips it; ``--git <path>`` injects a fake git (test seam).

Local-overlap liveness (#2623): "a worktree exists" said nothing about whether a
session is LIVE in it, so liveness was a manual second step (``session_lock.py
occupied``) and the line read byte-identical for a sibling mid-build, a merged
leftover and an abandoned husk. Each worktree line now carries its lock state:
``[session: OCCUPIED]`` (a live session IS building it), ``FREE (own lock)`` /
``FREE (lock holder dead)`` (a leftover, not a concurrent build), ``no lock``
(nothing claims it -- agent-* lanes write none, so the "may be building" hedge
stays) or ``unknown (…)`` (unreadable / unprovable, or a live lock while
``CLAUDE_CODE_SESSION_ID`` is unset, so it may be this session's own -- reported
as unknown, never as FREE, and to be treated as OCCUPIED). Read through session_lock.py's own
``read_lock`` + ``liveness`` (imported by path, the git_housekeeping seam), never
a hand-rolled pid probe: ``os.kill(pid, 0)`` TERMINATES the probed process on
Windows. Every LOCAL OVERLAP line and every HOLD block is stamped ``checked
<UTC ts>`` -- a rendered verdict cites live external state and has no expiry, so
a stale one has to read as stale rather than silently authoritative. Verdict
wording and exit codes are unchanged: still advisory, still never a HOLD.

No-lock recency (#2881): ``no lock`` is the usual state of a project-repo
worktree, so on its own it cannot tell a sibling mid-build from a husk. The
no-lock label therefore adds the newest mtime among the paths ``git status``
lists: ``no lock, edited <N>m ago`` (inside 30 min), ``no lock, idle <N>``,
``no lock, idle (clean tree)``, or ``no lock, recency unknown`` when the probe
cannot read the tree. Locked worktrees render as before, and exit codes do not
change.

Tiered file-overlap (#699, revised #833): an OPEN PR touching a target file always
HOLDs (a concurrent session is mid-flight on the same files). A MERGED PR touching
a target file is **advisory by default** (exit 0 + note) — a fast-follow PR in the
same epic legitimately touches the same files, and HOLDing every such follow-up is
a false positive (the 2026-06-19 #833 case: #826→#830 on ci_budget_sensor.py).
Pass ``--merged-window-min <N>`` (N>0) or ``--merged-since <iso>`` to opt-in to
the recency HOLD for an explicit window (test seam only — production never sets
this flag). Issue-CLOSED and issue→PR linkage stay hard HOLD regardless of age.

Self-describing verdict (#2733): the merged-PR advisories print newest-first and
ABOVE the verdict, and because they are advisory the exit code cannot carry them
-- so the verdict line names them itself: ``GO (3 merged-PR advisories above —
confirm the specific defect is still unfixed) — no remote overlap for <repo>#<n>``,
bare ``GO — no remote overlap for <repo>#<n>`` when there are none; same
qualifier on ``HOLD`` and ``UNDETERMINED``. A ``| tail -1`` view is enough to
know the caveats exist (the #2727 duplicate was filed past a ``| tail -6`` that
clipped the one advisory that mattered). Exit codes are unchanged.

Overlap-only pre-PR headline (#3413): the ``--pre-pr`` HOLD read ``<repo>#<n> is
already covered`` for every hold, but a file overlap measures shared files, not
coverage -- #3410 held on PR #3411's ``deploy/mac/jobs.conf`` while #3411 closed
#3367 and the two branches merged clean. When every hold is a file overlap the
opener reads ``HOLD — file overlap only (PR #<m>), not a link to <repo>#<n>: check
the hunks before filing:``. A CLOSED issue or a PR linking #<n> keeps "already
covered". Exit codes are unchanged.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# One definition of "a keyword quoted in code is not a closer" for both gates
# (#2981): fenced blocks and inline code spans become a sentinel before close_re
# runs, exactly as premerge_guard._closing_issues() does (#2976). Imported, not
# copied, so the two gates cannot drift apart again. Same directory, always run
# as a script, so sys.path[0] resolves it.
from premerge_guard import _strip_code

PREBRANCH_HOLD = 3  # exit code: a collision was detected, do NOT branch
# #2507 -- exit code: the target repo could not be resolved, so the remote overlap
# probe never ran. NOT a clean GO: an empty result there describes the INSTRUMENT,
# not the world, and returning 0 let a blind scan read as "checked, nothing found".
# Strictly below PREBRANCH_HOLD in precedence -- a hold is a positive finding, so a
# blind scan that still located an overlap blocks with 3 rather than softening to 4.
PREBRANCH_UNDETERMINED = 4

# An --issue value: a bare number (1609 -> cwd repo) or a fully-qualified
# cross-repo ref (octo/notes#31 -> issue 31 in octo/notes). A bare
# `#N` (no owner/repo) is rejected as ambiguous -- that ambiguity is the #1609 bug.
_ISSUE_REF_RE = re.compile(
    r"^(?:(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#)?(?P<num>\d+)$")


def _parse_issue_ref(raw):
    """Parse an --issue value into ``(number:int, repo:str|None)`` (#1609).

    ``"1609"`` -> ``(1609, None)`` (the cwd repo, unchanged from the bare-int
    behavior). ``"octo/notes#31"`` -> ``(31, "octo/notes")``. Raises
    ``ValueError`` on any other shape so a malformed ref fails loudly at argparse
    time rather than silently binding to the wrong repo."""
    m = _ISSUE_REF_RE.match((raw or "").strip())
    if not m:
        raise ValueError(
            "issue ref must be a bare number (1609) or owner/repo#N "
            "(octo/notes#31); got %r" % (raw,))
    return int(m.group("num")), m.group("repo")


def _gh_cmd(gh, args):
    """Build the gh argv. Test seam: a ``.py`` gh path is run under the current
    interpreter (a shebang script is not directly executable by subprocess on
    Windows), mirroring reconcile_prefetch.py."""
    if gh.endswith(".py"):
        return [sys.executable, gh, *args]
    return [gh, *args]


def _git_run(args, git="git"):
    """Run ``git <args>`` and return stdout (str), or None on any trouble.
    Fail-open. Test seam: a ``.py`` git path is run under the current interpreter
    (a fake git stub), mirroring ``_gh_cmd``."""
    try:
        argv = [sys.executable, git, *args] if git.endswith(".py") else [git, *args]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return None
        return proc.stdout or ""
    except Exception:
        return None


def _diff_files(base, git="git"):
    """Files this branch changes vs ``base`` (``git diff --name-only <base>...HEAD``).

    Ground truth for ``--pre-pr``'s file-overlap (#2013): at PR time the target set
    IS the diff, so it stops being the guess the module docstring's scope-split
    note describes. Fail-open -- ``[]`` on any trouble, so the caller keeps
    whatever ``--files`` it was given rather than silently narrowing the scan.
    ``[]`` is a null reading (nothing committed, or git failed), never "touches no
    files": ``_prebranch`` reports it as UNDETERMINED (#3047)."""
    out = _git_run(["diff", "--name-only", "%s...HEAD" % base], git)
    if out is None:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _gh_api_json(gh, path):
    """Parsed JSON from ``gh api <path>`` (REST), or None on any trouble. UTF-8
    explicitly: REST payloads carry whole PR/issue bodies, and a locale-codec
    decode can fail on the first emoji (the #2827 note in _gh_issue_state)."""
    try:
        proc = subprocess.run(_gh_cmd(gh, ["api", path]), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=30)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout or "null")
    except Exception:
        return None


def _gh_pr_list_rest(gh, state, limit, nwo):
    """REST twin of `gh pr list` (#2831), in its JSON shape, or None on any trouble.

    ``open``: ``/pulls?state=open`` plus one ``/pulls/<n>/files`` call per PR --
    the open file-overlap leg HOLDs, so an unreadable file list makes the whole
    listing unreadable (None), never a PR that silently touches nothing.
    ``merged``: ``/pulls?state=closed`` filtered to ``merged_at`` -- REST has no
    merged state, and a closed-unmerged PR is not a merged duplicate. Its files
    are NOT fetched (``files: None``): that would be one call per PR for a leg
    that is advisory-only (#833); _prebranch says so when it matters."""
    rest_state = "open" if state == "open" else "closed"
    per_page = min(100, limit) if state == "open" else 100
    raw = _gh_api_json(gh, "repos/%s/pulls?state=%s&sort=created&direction=desc"
                           "&per_page=%d" % (nwo, rest_state, per_page))
    if not isinstance(raw, list):
        return None
    prs = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        merged_at = d.get("merged_at")
        if state == "merged" and not merged_at:
            continue
        pr = {"number": d.get("number"), "title": d.get("title"),
              "url": d.get("html_url"),
              "state": "MERGED" if merged_at else str(d.get("state") or "").upper(),
              "body": d.get("body"), "mergedAt": merged_at,
              "createdAt": d.get("created_at"), "files": None}
        if state == "open":
            files = _gh_api_json(gh, "repos/%s/pulls/%s/files?per_page=100"
                                     % (nwo, pr["number"]))
            if not isinstance(files, list):
                return None
            pr["files"] = [{"path": f.get("filename") or ""} for f in files
                           if isinstance(f, dict)]
        prs.append(pr)
        if len(prs) >= limit:
            break
    return prs


def _gh_pr_list(gh, state, limit, repo=None, git="git"):
    """Return the parsed JSON array from `gh pr list` -- or its REST twin -- or
    None when NEITHER transport could list (#2831: None is not [], which means
    "listed, no PRs").

    ``files`` is requested so ``--prebranch`` can compute file-overlap;
    ``createdAt`` so ``--pre-pr --since`` can split build-window hits from
    pre-existing ones (#2013). The default advisory path parses only stdout lines
    and ignores the extra fields.

    ``repo`` (#2502) scopes the listing to a FOREIGN repo, exactly as
    ``_gh_issue_state`` already does. Without it this call had no repo at all and
    `gh` resolved one from the process cwd -- so a scan for other-repo#762 issued
    from a home-repo worktree read home-repo's open PRs and printed a
    bare ``GO``. Not an exotic misuse: #1609 already lets the ISSUE be qualified,
    and it scoped the issue-state query only, so a caller who did everything right
    still got one probe aimed at the target and one at the cwd, merged into a
    single verdict with nothing marking the seam. Under CLAUDE.md's
    session-workspace rule the two disagree by default, not by accident.
    """
    try:
        proc = subprocess.run(
            _gh_cmd(gh, ["pr", "list", "--state", state, "--limit", str(limit),
                         *(["--repo", repo] if repo else []),
                         "--json",
                         "number,title,url,state,body,files,mergedAt,createdAt"]),
            capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return json.loads(proc.stdout or "[]")
    except Exception:
        pass
    # #2831 -- `gh pr list` is GraphQL. #2827 gave the issue-state read a REST
    # twin but not this, so under a GraphQL-only limit a scan answered the issue
    # and read [] here -- "no PRs" -- and exited GO, blind in the leg that HOLDs on
    # a duplicate PR. Retry over REST; the repo is ``repo``, else the cwd's git
    # origin (never `gh repo view`, GraphQL too).
    nwo = repo or _nwo_from_url(_git_run(["remote", "get-url", "origin"], git))
    prs = _gh_pr_list_rest(gh, state, limit, nwo) if nwo else None
    if prs is not None:
        return prs
    print("note: could not list %s PRs over GraphQL or REST -- linkage/file-overlap "
          "vs %s PRs unchecked" % (state, state), file=sys.stderr)
    return None


def _matches(prs, issue, repo=None):
    """Lines for PRs whose body or title closing-references #issue.

    Body match requires a GitHub closing keyword (close/fix/resolve + forms) so a
    mere cross-reference ("see #N") does not trip it. Title match (#3385) takes
    two shapes. One is a closing keyword (``... (Closes #N)``): the title becomes
    the squash commit subject, and that subject closes the issue even when the
    body does not (PRs #3211-#3213). The other is a trailing ``(#N)``, the older
    squash-title form that names the closed issue. A ``#N`` anywhere else in the
    title is a prose citation, not a closer. PR #3370's title named #3107 as an
    example while closing #3366, and that HOLDed both scans for #3107's real
    fix. Measured on 300 merged PRs: every real closer still matches, and none
    of the 17 prose citations does. A merged PR's citation still prints under
    ``_sibling_mentions()``'s advisory note.

    Epic-tracker sibling exemption (#996): a long-lived epic tracker is closed by
    many sequential ``Part of #N`` PRs; every pole after the first cites the epic
    issue in its title, so the bare-`#N` title rule false-HOLDed on exactly the
    multi-PR epics it should support. A ``Part of #N`` / ``Refs #N`` / ``Related to
    #N`` reference is a *co-PR*, not a duplicate — it is excluded from the title
    match. A genuine closing keyword in the body still wins (that PR really did
    resolve the issue), so only the title match is gated.

    Cross-repo (#1609): ``repo`` set means the target issue lives in a DIFFERENT
    repo than these (cwd-repo) PRs, so a bare ``#N`` here is *cwd-repo* #N — a
    different issue — and must NOT match. A local PR that truly closes the foreign
    issue uses GitHub's qualified cross-repo form ``owner/repo#N``, so every match
    token is anchored to that qualified prefix instead of a bare ``#N``. ``repo``
    None keeps the bare-``#N`` behavior for a same-repo issue (unchanged).
    """
    tok = r"%s#%d\b" % (re.escape(repo), issue) if repo else r"#%d\b" % issue
    # [ \t]+, not \s+ -- a real closing ref is same-line; \s+ spans blank lines
    # and false-matches a "## What this does NOT close\n\n#N ..." heading (#1869).
    close_re = re.compile(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[ \t]+%s" % tok, re.I)
    # #3385: a title #N counts after a closing keyword or as the trailing "(#N)"
    # squash suffix, never as a bare citation anywhere in the title.
    suffix_re = re.compile(r"\(%s\)\s*$" % tok)
    sibling_re = re.compile(
        r"\b(?:part[\s-]+of|refs?|related\s+to)\s+%s" % tok, re.I)
    out = []
    for pr in prs:
        body = pr.get("body") or ""
        title = (pr.get("title") or "").strip()
        if close_re.search(_strip_code(body)):  # quoted-in-code is not a closer (#2981)
            pass  # genuine closer — a real duplicate signal regardless of title
        elif (close_re.search(title) or suffix_re.search(title)) and not (
                sibling_re.search(title) or sibling_re.search(body)):
            pass  # title closer with no sibling marker — treat as a dup
        else:
            continue  # no closing ref, or only a sibling (Part of / Refs) ref
        out.append("#{n} {t} [{s}] {u}".format(
            n=pr.get("number"), t=title, s=pr.get("state"),
            u=pr.get("url") or ""))
    return out


def _sibling_mentions(prs, issue, repo=None):
    """Lines for MERGED PRs that mention #issue WITHOUT a genuine closing keyword --
    a sibling co-PR ("Part of #N" / "Refs #N" / "Related to #N") or any other bare
    non-closing mention (e.g. "see #N"). Purely informational (#1756): a strict
    superset of ``_matches()``'s ``sibling_re`` exemption (#996), which excludes
    this exact shape from the HOLD/duplicate signal on purpose (a long-lived epic
    tracker closed by many sequential "Part of #N" poles must not false-HOLD) but
    leaves the caller with zero signal that a sibling PR exists at all -- the gap
    that let #1700/#1704 (PR #1704 merged "Part of #1700", already shipping 2 of 3
    AC items) go undetected by a state-only re-verify.

    Unlike ``_matches()``'s title check (a closing keyword or a trailing ``(#N)``,
    #3385), ``ref_re`` here is a bare ``#N`` tested against BOTH title and body.
    So a mention that lives only in the body still surfaces, and so does a prose
    citation in the title, which ``_matches()`` no longer counts.

    A PR that ``_matches()`` returns is skipped whatever made it a closer: its
    match line already lists it, and this note says it has no closing keyword.
    Skipping only a body closer listed a title-only closer twice (#3455).
    """
    tok = r"%s#%d\b" % (re.escape(repo), issue) if repo else r"#%d\b" % issue
    ref_re = re.compile(tok)
    out = []
    for pr in prs:
        body = pr.get("body") or ""
        title = (pr.get("title") or "").strip()
        if _matches([pr], issue, repo):
            continue  # a closer -- already printed as a _matches() line
        if ref_re.search(title) or ref_re.search(body):
            out.append("#{n} {t} [{s}] {u}".format(
                n=pr.get("number"), t=title, s=pr.get("state"),
                u=pr.get("url") or ""))
    return out


# ── base-freshness advisory (#2219) ─────────────────────────────────────────
# Base freshness was checked at MERGE time and nowhere earlier: premerge_guard's
# evaluate() STALE-HOLDs any behind_by > 0, on the #841->#842 reasoning that a
# green which ran against a base `main` has since moved past is not evidence
# about the base it will land on. Nothing asked the question before that, so a PR
# filed off a base that moved during the build was structurally undetectable
# until `mgo` -- by which point it had burned a CI run against the wrong base and
# the author had context-switched away. --pre-pr already runs at the right
# moment; it simply asked a different question (#2013's duplicate-work checks).
#
# Deliberately ADVISORY, never a HOLD: the remedy is one server-side call, and a
# PR should exist even if its base moved. The exit code stays the duplicate
# checks' to decide.
_PREMERGE_GUARD = []  # lazy single-slot cache: [] = unloaded, [None] = failed


def _premerge_guard():
    """The ``premerge_guard`` module, or None when it cannot be imported.

    Lazy and fail-open by design. dup_scan is stdlib-only and runs in the
    preflight of every build, so a sibling script that is missing, renamed, or
    syntactically broken must degrade this one advisory to silence rather than
    take the whole preflight down with it."""
    if not _PREMERGE_GUARD:
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            if here not in sys.path:
                sys.path.insert(0, here)
            import premerge_guard  # noqa: PLC0415 - deliberately lazy
            _PREMERGE_GUARD.append(premerge_guard)
        except Exception:
            _PREMERGE_GUARD.append(None)
    return _PREMERGE_GUARD[0]


def _branch_name(base):
    """``origin/main`` -> ``main``: the remote BRANCH both transports need.

    ``--base`` is spelled as a remote-tracking ref because that is what
    ``git diff <base>...HEAD`` takes. Neither consumer here accepts that spelling:
    ``repos/<r>/compare/origin/main...<head>`` 404s, and the git refspec
    ``+refs/heads/origin/main:...`` matches nothing. Both failures are silent and
    both read downstream as "fresh" -- a false negative in exactly the case this
    check exists for -- so the prefix is stripped once, here."""
    ref = (base or "").strip()
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _compare_behind(gh, repo, base, head):
    """``behind_by`` over the GitHub compare API. RAISES when the answer cannot be
    trusted, so the caller falls through to the git transport.

    A missing/garbled payload must not reach ``behind_from_compare``, which maps
    anything non-dict to 0 -- correct for its own caller (which raises first) but
    here it would turn a blind probe into a confident "not behind"."""
    proc = subprocess.run(
        _gh_cmd(gh, ["api", "repos/%s/compare/%s...%s" % (repo, base, head)]),
        capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("compare probe failed: %s" % (proc.stderr or "").strip())
    payload = json.loads(proc.stdout or "null")
    if not isinstance(payload, dict) or "behind_by" not in payload:
        raise RuntimeError("compare payload carries no behind_by")
    return _premerge_guard().behind_from_compare(payload)


def _local_behind(base, git="git"):
    """``behind_by`` measured entirely locally: commits on ``origin/<base>`` that
    the working checkout's HEAD does not have. None when it cannot be trusted.

    The third transport, and the only one that answers at ``--pre-pr`` time in the
    COMMON case. Both remote transports resolve the head ref on origin, but at
    this moment the branch usually is not pushed yet -- so both correctly decline
    and the advisory degrades to UNDETERMINED on the majority of the runs it
    exists for. A guard whose firing condition can rarely be true in the running
    configuration is a guard that never fires; reachable is not the same as
    effective.

    The head this needs is not a remote ref at all: it is the local HEAD that is
    about to BECOME the PR's head. Only the BASE has to come from origin, and it
    is re-fetched under an explicit refspec rather than read from whatever the
    checkout happens to hold -- a stale ``refs/remotes/origin/<base>`` would
    answer with yesterday's graph and look identical to a fresh reading."""
    if not base:
        return None
    if _git_run(["fetch", "--quiet", "origin",
                 "+refs/heads/%s:refs/remotes/origin/%s" % (base, base)],
                git) is None:
        return None
    out = _git_run(["rev-list", "--count",
                    "HEAD..refs/remotes/origin/%s" % base], git)
    try:
        return int((out or "").strip())
    except ValueError:  # non-numeric stdout -> decline, never guess
        return None


def measure_behind(repo, base, head, gh="gh", git="git"):
    """``(behind_by, transport)``, or ``(None, None)`` when none can measure.

    Three transports. The first two are the #2150 pair -- that issue exists
    because the compare endpoint 404'd for ~4.5h and reported a 0-behind PR four
    times running -- and both answer about the head as it exists ON ORIGIN, which
    is what GitHub will actually compare once the PR is filed. They are tried
    first for that reason.

    ``_local_behind`` is the fallback that covers the un-pushed branch, i.e. most
    of ``--pre-pr``. It needs neither ``repo`` nor ``head`` nor premerge_guard, so
    it still runs when the lazy import failed -- one more way this degrades to an
    answer rather than to silence.

    ``None`` is UNDETERMINED and is never collapsed into 0 -- the caller says so
    aloud."""
    pg = _premerge_guard()
    if pg is not None and repo and base and head:
        try:
            return (_compare_behind(gh, repo, base, head), "compare API")
        except Exception:
            pass  # transport A blind -> git protocol, never a guess
        behind = pg.behind_via_git(repo, base, head)
        if behind is not None:
            return (behind, "git")
    behind = _local_behind(base, git)
    return (behind, "local git") if behind is not None else (None, None)


def _base_freshness_advisory(args):
    """Print the --pre-pr base-freshness advisory. Never returns a verdict and
    never touches ``holds`` -- the exit code stays the duplicate checks'."""
    base = _branch_name(getattr(args, "base", "origin/main")) or "main"
    head = (_git_run(["rev-parse", "--abbrev-ref", "HEAD"], args.git) or "").strip()
    repo = _cwd_repo(args.gh)
    behind, transport = measure_behind(repo, base, head, args.gh, args.git)
    if behind is None:
        # A null measures the INSTRUMENT first: silence here would be
        # indistinguishable from the fresh-base silence below.
        print("note — base freshness: could not measure %s vs %s over any "
              "transport (compare API, git, local). UNDETERMINED, not fresh — "
              "re-check with `git fetch origin && git log --oneline "
              "HEAD..origin/%s`." % (head or "HEAD", base, base))
        return
    if behind <= 0:
        return
    print("BASE STALE (advisory, not a HOLD) — %s is %d commit(s) behind %s "
          "(measured over %s)." % (head or "HEAD", behind, base, transport))
    print("  Its CI would run against a base %s has already moved past, so `mgo` "
          "will STALE-HOLD it (the #841->#842 class)." % base)
    print("  Fix BEFORE filing (no PR number exists yet): "
          "git fetch origin && git merge origin/%s" % base)
    print("  Equivalent AFTER filing: gh api --method PUT "
          "repos/%s/pulls/<n>/update-branch" % (repo or "<owner>/<repo>"))


def _norm(path):
    """Normalize a repo path for set-comparison: backslashes -> /, strip a
    leading ``./``. Mirrors gitignore_guard's token normalization (#679)."""
    p = (path or "").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _git_fetch():
    """Best-effort `git fetch` to refresh the (stale) turn-start view. Fail-open."""
    try:
        subprocess.run(["git", "fetch", "origin", "--quiet"],
                       capture_output=True, timeout=30)
    except Exception:
        pass


def _nwo_from_url(url):
    """``owner/name`` from an SSH or HTTPS remote URL; None if unrecognised.
    Same pattern as premerge_guard's."""
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", (url or "").strip())
    return "%s/%s" % (m.group(1), m.group(2)) if m else None


def _gh_issue_state(gh, issue, repo=None, git="git"):
    """Return the issue's state dict (state/title/url), or None on any trouble.

    ``repo`` set (#1609) scopes the query to that repo via ``--repo <owner/name>``
    so a cross-repo target (octo/notes#31) is read from the FOREIGN repo
    instead of silently binding to the cwd repo's own #N.

    #2827 -- `gh issue view` is GraphQL. Under a GraphQL-only limit it failed
    while REST answered, this returned None, and _prebranch read that as "gh is
    down": the CLOSED-issue HOLD was skipped for a fail-open GO. A failed GraphQL
    read now retries once over ``gh api repos/<nwo>/issues/<n>`` (the #2823 /
    #2820 pattern). ``<nwo>`` is ``repo``, else the cwd's git origin -- never
    `gh repo view`, which is GraphQL too. None still means BOTH transports failed,
    so a total outage keeps the documented fail-open GO."""
    try:
        proc = subprocess.run(
            _gh_cmd(gh, ["issue", "view", str(issue),
                         *(["--repo", repo] if repo else []),
                         "--json", "state,title,url"]),
            capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return json.loads(proc.stdout or "{}")
    except Exception:
        pass
    nwo = repo or _nwo_from_url(_git_run(["remote", "get-url", "origin"], git))
    if not nwo:
        return None
    try:
        # UTF-8 explicitly: REST returns the whole issue, body included, and a
        # locale-codec (cp1255 here) decode of it can fail on the first emoji.
        proc = subprocess.run(
            _gh_cmd(gh, ["api", "repos/%s/issues/%d" % (nwo, issue)]),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30)
        if proc.returncode != 0:
            return None
        d = json.loads(proc.stdout or "{}")
    except Exception:
        return None
    # REST /issues serves pull requests too; a PR's state is not the issue's.
    if not isinstance(d, dict) or "number" not in d or d.get("pull_request"):
        return None
    return {"state": str(d.get("state") or "").upper(), "title": d.get("title"),
            "url": d.get("html_url")}


def _cwd_repo(gh, git="git"):
    """The cwd repo's ``owner/name`` (``gh repo view --json nameWithOwner``), or
    None on any trouble. Used to decide same-repo vs cross-repo issue matching.

    #2831 -- `gh repo view` is GraphQL. Under a GraphQL-only limit it returned
    None, so a QUALIFIED same-repo scan (``--issue owner/name#N``, the form the
    CLAUDE.md gate prescribes) was taken for cross-repo and matched only the
    qualified token: a bare ``Closes #N`` duplicate read as no match. Falls back
    to the cwd's git origin -- plain git, no GraphQL."""
    try:
        proc = subprocess.run(
            _gh_cmd(gh, ["repo", "view", "--json", "nameWithOwner"]),
            capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            name = (json.loads(proc.stdout or "{}").get("nameWithOwner") or "").strip()
            if name:
                return name
    except Exception:
        pass
    return _nwo_from_url(_git_run(["remote", "get-url", "origin"], git))


def _scanned_repo_label(gh, issue_repo, git="git"):
    """``"owner/name"`` for the verdict line, or ``""`` when it cannot be resolved.

    Every verdict names the repo it actually scanned (#2502). The flag that fixes
    a wrong-repo scan only helps a caller who already knows the scan is
    cwd-bound; the person who hits this does not, and the verdict line is the
    only signal they have. Naming it turns a silently wrong answer into an
    obviously wrong one for the price of one string.

    Unresolvable -> "", i.e. the line degrades to its pre-#2502 wording rather
    than printing a guess. An unknown repo must not read as a known one.
    """
    repo = issue_repo or _cwd_repo(gh, git)
    return ("%s" % repo) if repo else ""


def _match_repo(gh, issue_repo, git="git"):
    """The repo qualifier to use when MATCHING the issue number against LOCAL PRs
    and commits, or None to use bare ``#N`` matching (#1609).

    A bare ``#N`` in a local PR/commit refers to the CWD repo's #N; when the target
    issue lives in a DIFFERENT repo, bare matching binds to the wrong issue, so the
    qualified ``owner/repo#N`` form must be matched instead. Same-repo (no
    qualifier, or a qualifier equal to the cwd repo) keeps bare matching. Fail-safe:
    if the cwd repo cannot be determined, a qualified ref is treated as cross-repo —
    qualified matching never false-matches a bare cwd ``#N``, so the safe direction
    is to require the qualified form rather than risk a wrong-repo verdict."""
    if not issue_repo:
        return None  # bare number -> cwd repo, unchanged (no extra gh call)
    cwd = _cwd_repo(gh, git)
    if cwd is not None and issue_repo.lower() == cwd.lower():
        return None  # qualified but same repo -> bare matching is correct
    return issue_repo


def _parse_files(file_args):
    """Flatten repeated/comma-separated --files into a normalized set."""
    out = set()
    for chunk in file_args or []:
        for piece in chunk.split(","):
            piece = piece.strip()
            if piece:
                out.add(_norm(piece))
    return out


def _file_overlap(prs, targets):
    """For each PR, the sorted intersection of its changed files with the target
    set. Returns [(pr, [overlapping_paths])] for non-empty intersections."""
    hits = []
    for pr in prs:
        files = {_norm(f.get("path", "")) for f in (pr.get("files") or [])}
        inter = sorted(files & targets)
        if inter:
            hits.append((pr, inter))
    return hits


def _parse_iso(ts):
    """Parse a GH ISO-8601 timestamp (e.g. ``2026-06-16T11:33:00Z``) to a tz-aware
    UTC datetime, or None if absent/unparseable."""
    if not ts:
        return None
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


_LINE_NUM_RE = re.compile(r"^#(\d+)\b")


def _window_label(line, created_by_num, since):
    """`` [build-window: created <ts>]`` / `` [pre-existing: created <ts>]`` for a
    ``_matches()`` line, or ``""`` when unlabelable (#2013).

    Splits a collision that appeared DURING this build -- the class ``--prebranch``
    structurally cannot catch, where the tooling worked exactly as designed -- from
    one that predates the branch point, which IS a real ``--prebranch`` miss. A
    retro reads those two oppositely (accept the race vs. fix the detector), so an
    unlabeled hit invites the wrong conclusion. Fail-open: no ``--since``, no
    ``createdAt``, or an unparseable one yields no label rather than a guess."""
    if since is None:
        return ""
    m = _LINE_NUM_RE.match(line)
    if not m:
        return ""
    created = _parse_iso(created_by_num.get(int(m.group(1))))
    if created is None:
        return ""
    kind = "build-window" if created >= since else "pre-existing"
    return " [%s: created %s]" % (kind, created.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _merged_cutoff(args):
    """The UTC instant at/after which a merge counts as 'recent' (the build
    window). ``--merged-since`` (explicit ISO) wins; otherwise ``now -
    --merged-window-min`` minutes. Returns None to disable the recency HOLD
    (``--merged-window-min 0`` with no ``--merged-since``) so every merged overlap
    becomes advisory."""
    if args.merged_since:
        dt = _parse_iso(args.merged_since)
        if dt is not None:
            return dt
    win = args.merged_window_min
    if win is None or win <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(minutes=win)


def _merged_is_recent(pr, cutoff):
    """True if ``pr`` merged at/after ``cutoff`` (inside the build window).
    ``cutoff`` None ⇒ recency HOLD disabled, nothing counts as recent. A missing/
    unparseable ``mergedAt`` with a cutoff set ⇒ treated as recent: we can't prove
    it's old, so don't suppress the #678-class build-window HOLD."""
    if cutoff is None:
        return False
    merged_at = _parse_iso(pr.get("mergedAt"))
    if merged_at is None:
        return True
    return merged_at >= cutoff


# ── local-overlap liveness (#2623) ──────────────────────────────────────────
SESSION_LOCK_NAME = ".session-lock"
_HEDGE = "a concurrent session may be building it"


def _scan_stamp():
    """The scan's own read time, UTC to the second. Printed on every LOCAL
    OVERLAP line and under every HOLD block (#2623): both cite live external
    state (a worktree's lock, an OPEN PR) and neither expires on its own."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_session_lock():
    """Import ``session_lock.py`` by path -- the scripts dir is not a package, and
    this is the seam git_housekeeping already uses. ``None`` on any failure; the
    caller then reports ``unknown``, never FREE (#2623)."""
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_lock.py")
        spec = importlib.util.spec_from_file_location("session_lock", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_RECENT_EDIT_WINDOW_S = 30 * 60


def _age_text(seconds):
    """``45m`` / ``3h`` / ``2d``: minutes below two hours, then hours, then days."""
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 120:
        return "%dm" % minutes
    hours = minutes // 60
    return "%dh" % hours if hours < 48 else "%dd" % (hours // 24)


def _edit_recency(wt_path, git="git"):
    """When a lockless worktree's tree was last edited (#2881): the newest mtime
    among the paths ``git status`` lists (modified or untracked). ``edited <N>m
    ago`` inside ``_RECENT_EDIT_WINDOW_S``, ``idle <N>`` past it, ``idle (clean
    tree)`` when status lists nothing, and ``recency unknown`` when status fails
    or no listed path can be stat'd (a deletion-only tree) -- an unread probe
    never reads as idle.

    ``--no-optional-locks``: the probed tree may be a live sibling's, and a plain
    ``git status`` refreshes its index under ``index.lock`` -- the scan must not
    collide with the build it is looking at. ``-z`` keeps paths unquoted; a
    rename/copy entry is followed by its source path, which is skipped."""
    out = _git_run(["-C", wt_path, "--no-optional-locks", "status", "--porcelain",
                    "-z", "--untracked-files=all"], git)
    if out is None:
        return "recency unknown"
    fields = out.split("\0")
    listed = False
    newest = None
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        listed = True
        if "R" in entry[:2] or "C" in entry[:2]:
            i += 1
        try:
            mtime = os.stat(os.path.join(wt_path, entry[3:])).st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        return "recency unknown" if listed else "idle (clean tree)"
    age = time.time() - newest
    if age < _RECENT_EDIT_WINDOW_S:
        return "edited %s ago" % _age_text(age)
    return "idle %s" % _age_text(age)


def _session_state(wt_path, self_session_id, sl, git="git"):
    """``(label, hedge)`` for the worktree at ``wt_path`` (#2623): ``label`` goes
    into ``[session: …]``, ``hedge`` is the line's closing clause.

    Composes session_lock.py's ``read_lock`` + ``liveness`` rather than its
    ``occupancy``: occupancy answers "may I take this worktree?" and folds an
    UNREADABLE lock into FREE (read_lock -> None -> FREE), which is exactly the
    silent-FREE the advisory must not print -- a null reading measures the
    instrument first. Absent and unreadable are split here for the same reason.
    Never a hand-rolled pid probe: ``liveness`` owns that (OpenProcess on
    Windows; ``os.kill(pid, 0)`` would TerminateProcess the probed session)."""
    if sl is None:
        return "unknown (session_lock.py unavailable)", _HEDGE + " -- treat as OCCUPIED"
    lock_path = os.path.join(wt_path, SESSION_LOCK_NAME)
    if not os.path.exists(lock_path):
        return ("no lock, " + _edit_recency(wt_path, git),
                _HEDGE + " (nothing claims the worktree; agent-* lanes write no lock)")
    lock = sl.read_lock(lock_path)
    if lock is None:
        return "unknown (lock unreadable)", _HEDGE + " -- treat as OCCUPIED"
    if self_session_id and str(lock.get("session_id")) == str(self_session_id):
        return "FREE (own lock)", "this session's own worktree, not a concurrent build"
    state = sl.liveness(lock)
    if state == sl.ALIVE and not self_session_id:
        # No CLAUDE_CODE_SESSION_ID: a live lock may be this session's own, so
        # "IS building it" would be a guess about who holds it.
        return "unknown (no session id)", _HEDGE + " -- treat as OCCUPIED"
    if state == sl.ALIVE:
        return "OCCUPIED", "a concurrent session IS building it"
    if state == sl.DEAD:
        return "FREE (lock holder dead)", "a leftover worktree, not a concurrent build"
    return "unknown (liveness unprovable)", _HEDGE + " -- treat as OCCUPIED"


def _local_overlap(issue, git="git", repo=None):
    """Advisory notes for any LOCAL worktree/branch that appears to build the same
    issue (#1070). The remote checks (issue-state, PR linkage, file-overlap) are
    blind to a concurrent session's *unpushed* worktree or branch -- the gap that
    let ``build-1055`` build #1055 invisibly while a second session also took it.

    Returns a list of advisory note strings (NEVER a HOLD): a legitimate second
    worktree on a related-but-different branch must not be blocked, so this surfaces
    loudly and lets the human judge. Fail-open: any git trouble yields []. The
    current branch is excluded (self-match) so a ``--prebranch`` run from inside the
    build worktree for #issue does not flag itself.

    Each worktree note names the worktree's lock state ``[session: …]`` and the
    scan's read time ``checked <UTC ts>`` (#2623, see ``_session_state``): a
    live lock is the case that actually costs something, a dead one is a
    leftover, and the two used to print byte-identical.

    A branch/worktree "references" the issue when its slug carries the number as a
    delimited token (``build-1055``, ``feat/x-1055``, ``fix/1055-y``) -- boundary-
    anchored so ``811`` never matches ``81`` and ``10551`` never matches ``1055`` --
    or, for a detached worktree, when its HEAD commit subject names ``#issue``.

    Cross-repo (#1609): ``repo`` set means a bare ``#N`` in a LOCAL commit subject
    is cwd-repo #N (a different issue), so the commit-subject match is anchored to
    the qualified ``owner/repo#N`` form. Branch *slugs* carry no repo, so slug
    matching stays bare (a build-<N> branch is a repo-agnostic hint) -- acceptable
    because this whole path is a loud advisory, never a HOLD.
    """
    slug_re = re.compile(r"(?:^|[-_/])%d(?:[-_/]|$)" % issue)
    msg_re = re.compile(r"%s#%d(?![0-9])" % (re.escape(repo), issue) if repo
                        else r"#%d(?![0-9])" % issue)
    notes = []
    cur = (_git_run(["rev-parse", "--abbrev-ref", "HEAD"], git) or "").strip()
    flagged = set()  # branch names already surfaced (avoid worktree+branch dup)
    sl = _load_session_lock()
    self_id = None
    if sl is not None:
        try:
            self_id = sl._default_session_id()   # CLAUDE_CODE_SESSION_ID, else None
        except Exception:
            self_id = None
    stamp = _scan_stamp()

    porc = _git_run(["worktree", "list", "--porcelain"], git)
    if porc:
        for block in re.split(r"\n\s*\n", porc.strip()):
            path = head = branch = None
            for line in block.splitlines():
                if line.startswith("worktree "):
                    path = line[len("worktree "):].strip()
                elif line.startswith("HEAD "):
                    head = line[len("HEAD "):].strip()
                elif line.startswith("branch "):
                    branch = line[len("branch "):].strip()
                    if branch.startswith("refs/heads/"):
                        branch = branch[len("refs/heads/"):]
            if path is None:
                continue
            bname = branch or ""
            if bname and bname == cur:
                continue  # self
            hit_slug = bool(bname and slug_re.search(bname))
            via_msg = False
            if not hit_slug and head:
                subj = _git_run(["log", "-1", "--format=%s", head], git) or ""
                via_msg = bool(msg_re.search(subj))
            if hit_slug or via_msg:
                if bname:
                    flagged.add(bname)
                why = "branch slug" if hit_slug else "HEAD commit"
                label, hedge = _session_state(path, self_id, sl, git)
                notes.append("worktree %s (%s) [session: %s] references #%d via %s "
                             "-- %s (checked %s)"
                             % (path, bname or "detached HEAD", label, issue, why,
                                hedge, stamp))

    refs = _git_run(["for-each-ref", "--format=%(refname:short)", "refs/heads"], git)
    if refs:
        for name in refs.splitlines():
            name = name.strip()
            if not name or name == cur or name in flagged:
                continue
            if slug_re.search(name):
                notes.append("local branch %s encodes #%d but is unpushed/un-PR'd "
                             "(built-not-pushed?)" % (name, issue))
    return notes


def _prebranch(args):
    """Run the three immediately-before-branching checks; HOLD (exit 3) on any
    collision, GO (exit 0) when clean. Fail-open on infra trouble. A LOCAL
    worktree/branch overlap (#1070) is surfaced as a loud advisory but never
    changes the HOLD/GO exit code.

    ``--pre-pr`` (#2013) runs these SAME checks a second time, immediately before
    the PR is opened -- the detection is already correct, it was just never asked
    twice. Only the target-set derivation and the wording of the verdict differ."""
    pre_pr = getattr(args, "pre_pr", False)
    if not args.no_fetch:
        _git_fetch()
    holds = []

    # #1609: decide same-repo (bare #N) vs cross-repo (qualified owner/repo#N)
    # matching once. issue_repo (from the ref) scopes the issue-state query to the
    # FOREIGN repo; match_repo qualifies the LOCAL PR/commit matching (None = bare).
    issue_repo = getattr(args, "issue_repo", None)
    match_repo = _match_repo(args.gh, issue_repo, args.git)

    state = _gh_issue_state(args.gh, args.issue, issue_repo, args.git)
    # #2507 -- this doubles as the liveness probe that separates "gh is down" from
    # "gh works, the repo just could not be resolved". Both leave the repo label
    # empty, but only the second earns UNDETERMINED: a total gh outage keeps the
    # documented fail-open GO (see the module docstring / case (f)), because
    # revoking fail-open is a broader contract change than this issue's scope.
    gh_alive = state is not None
    if state is None:
        print("note: could not verify issue #%d state (fail-open)" % args.issue,
              file=sys.stderr)
    elif (state.get("state") or "").upper() == "CLOSED":
        holds.append("issue #%d is CLOSED (%s) — already resolved"
                     % (args.issue, state.get("url") or ""))

    # Fetch both sets once; reuse for linkage AND file-overlap. Linkage covers
    # MERGED too: a PR that closes/references the exact issue number is a duplicate
    # signal regardless of age (it names #issue, not just a shared file).
    open_prs = _gh_pr_list(args.gh, "open", 50, issue_repo, args.git)
    merged_prs = _gh_pr_list(args.gh, "merged", 30, issue_repo, args.git)
    # #2831: None = unreadable on BOTH transports, [] = listed and empty. A blind
    # listing is a HOLD-bearing leg that never ran, decided after `holds` below.
    blind = [s for s, prs in (("open", open_prs), ("merged", merged_prs))
             if prs is None]
    open_prs, merged_prs = open_prs or [], merged_prs or []
    if args.skip_pr:
        open_prs   = [p for p in open_prs   if p.get("number") != args.skip_pr]
        merged_prs = [p for p in merged_prs if p.get("number") != args.skip_pr]
    created_by_num = {p.get("number"): p.get("createdAt")
                      for p in open_prs + merged_prs}
    since = _parse_iso(getattr(args, "since", None)) if pre_pr else None
    for line in _matches(open_prs + merged_prs, args.issue, match_repo):
        holds.append("a PR already references/closes #%d: %s%s"
                     % (args.issue, line,
                        _window_label(line, created_by_num, since)))

    # Tiered file-overlap (#699, revised #833). OPEN overlap = a concurrent in-flight
    # session on the same files -> always HOLD. MERGED overlap is advisory by default:
    # a fast-follow PR legitimately touches the same files, and recency-bounding caused
    # false-positive HOLDs on same-epic fast-follows (#833: #826→#830). Opt-in to the
    # recency window via --merged-window-min (N>0) or --merged-since (test seam).
    notes = []
    targets = _parse_files(args.files)
    # #3047: base of a derivation that came out EMPTY, else None (decided below).
    empty_derived_base = None
    if pre_pr and not targets:
        # #2013: at PR time the target set is ground truth, not a guess -- the
        # scope-split limitation in the module docstring only binds pre-branch.
        base = getattr(args, "base", "origin/main")
        targets = _parse_files(_diff_files(base, args.git))
        if targets:
            print("note — target set derived from the branch diff vs %s "
                  "(%d file(s))" % (base, len(targets)))
        else:
            # #3047: `...HEAD` sees commits only, and _diff_files is fail-open, so
            # uncommitted work and a git failure both land here. Silence made the
            # open-PR file-overlap leg -- the one --pre-pr exists for -- read as
            # checked-and-clean when it had nothing to intersect.
            empty_derived_base = base
            print("note — target set EMPTY (nothing committed vs %s, or git "
                  "failed); open-PR file-overlap NOT checked" % base)
    if targets and any(p.get("files") is None for p in merged_prs):
        # #2831: a REST-listed merged PR carries no files (see _gh_pr_list_rest).
        # The leg is advisory, so the verdict stands -- but say it was not checked
        # rather than print an advisory-free verdict as if it had been.
        print("note: merged PRs were listed over REST without their files -- "
              "merged-PR file overlap unchecked (advisory leg, #833; linkage still "
              "checked)", file=sys.stderr)
    # #3413: the PRs held on a shared file alone. When they are every hold, the
    # --pre-pr verdict names them instead of claiming #issue is covered.
    overlap_prs = []
    if targets:
        for pr, inter in _file_overlap(open_prs, targets):
            overlap_prs.append(pr.get("number"))
            holds.append("OPEN PR #%s changed files intersect the target set: %s [%s]"
                         % (pr.get("number"), ", ".join(inter), pr.get("url") or ""))
        cutoff = _merged_cutoff(args)
        # #2733: newest-first. Recency is what makes a merged-PR advisory worth
        # reading, and nothing here sorted -- the list came out in whatever order
        # `gh pr list` returned it. ISO-8601 Z timestamps sort lexicographically;
        # an undated PR sorts last (it is already the conservative HOLD case when
        # a recency window is set).
        for pr, inter in sorted(_file_overlap(merged_prs, targets),
                                key=lambda hit: hit[0].get("mergedAt") or "",
                                reverse=True):
            ctx = "%s, %s" % (", ".join(inter), pr.get("url") or "")
            if _merged_is_recent(pr, cutoff):
                overlap_prs.append(pr.get("number"))
                holds.append("recently-MERGED PR #%s (mergedAt %s) intersects the "
                             "target set: %s"
                             % (pr.get("number"), pr.get("mergedAt") or "?", ctx))
            else:
                notes.append("MERGED PR #%s (mergedAt %s) touched a target file "
                             "earlier — confirm the specific defect is still "
                             "unfixed: %s"
                             % (pr.get("number"), pr.get("mergedAt") or "?", ctx))

    if notes:
        print("note — older merged history touches the target set (advisory, not a "
              "HOLD):")
        for n in notes:
            print("  - " + n)

    # Sibling/non-closing-mention advisory (#1756): a merged PR mentions #issue
    # without a genuine closing keyword (_matches()'s sibling_re exemption, #996,
    # intentionally keeps it out of `holds`). Never a HOLD reason -- informational
    # only, so the caller reads the PR's body/AC-status before treating #issue as
    # buildable.
    sibling = _sibling_mentions(merged_prs, args.issue, match_repo)
    if sibling:
        print("note — merged PR(s) mention #%d without a closing keyword "
              "(informational — read the PR body/AC-status before treating #%d "
              "as buildable):" % (args.issue, args.issue))
        for s in sibling:
            print("  - " + s)

    # Local-overlap advisory (#1070): unpushed worktree/branch building the same
    # issue. Loud, but NEVER a HOLD — printed regardless of the GO/HOLD decision and
    # without touching `holds`, so the exit code is decided by the remote checks only.
    if not args.no_local:
        for n in _local_overlap(args.issue, args.git, match_repo):
            print("LOCAL OVERLAP (advisory, not a HOLD) for #%d — %s"
                  % (args.issue, n))

    # Base-freshness advisory (#2219) — --pre-pr only. Pre-BRANCH the branch has
    # no commits yet, so "behind" is not a meaningful question there. Like the
    # local-overlap block above, it prints regardless of the GO/HOLD decision and
    # never appends to `holds`.
    if pre_pr and not getattr(args, "no_base_check", False):
        _base_freshness_advisory(args)

    scanned = _scanned_repo_label(args.gh, issue_repo, args.git)
    # #2733: the verdict line names the merged-PR advisories printed above it.
    # They are advisory by design (#833), so the exit code cannot carry them --
    # and a verdict that reads identically with and without caveats attached
    # cannot survive truncation. `| tail -6` on a 20-line preflight clipped the
    # one advisory that mattered and turned a qualified GO into a clean one (the
    # #2727 duplicate). Bare wording is unchanged when there are none, so the
    # qualifier stays a signal. Wording only: exit codes are untouched.
    tag = _advisory_tag(len(notes))
    if holds:
        if pre_pr and len(overlap_prs) == len(holds):
            # #3413: a file overlap measures shared files, not coverage. #3410 held
            # on #3411's jobs.conf and read "already covered" while #3411 closed
            # #3367 and the hunks merged clean. Wording only: exit code unchanged.
            print("HOLD%s — file overlap only (PR %s), not a link to %s#%d: check "
                  "the hunks before filing:"
                  % (tag, ", ".join("#%s" % n for n in overlap_prs), scanned,
                     args.issue))
        elif pre_pr:
            print("HOLD%s — %s#%d is already covered; do NOT open a duplicate PR:"
                  % (tag, scanned, args.issue))
        else:
            print("HOLD%s — do NOT branch on %s#%d:" % (tag, scanned, args.issue))
        for h in holds:
            print("  - " + h)
        if pre_pr:
            # The build is already paid for, so "don't branch" is dead advice here
            # and aborting only strands the work. Name the four live options.
            print("  (the work already exists — choose: file as a competing "
                  "implementation, rebase down to the delta, discard, or harvest "
                  "the better parts. ADVISORY: do not auto-abort a finished build.)")
        else:
            print("  (re-verify manually; mergeability is only the last-resort "
                  "backstop.)")
        # #2623 (comment B): a HOLD cites live external state -- an OPEN PR, a
        # CLOSED issue -- and nothing re-evaluates it once rendered. A finished
        # build sat parked on a HOLD naming a PR that had merged 30 min earlier.
        # Date the verdict so an old one reads as old; the opener stays bare.
        print("  (checked %s -- a HOLD has no expiry: re-run the scan before acting "
              "on one you did not just render)" % _scan_stamp())
        return PREBRANCH_HOLD
    # #2507 -- checked only AFTER `holds`, so a positive finding still wins. An
    # empty `scanned` means _cwd_repo() could not answer and the issue carried no
    # qualifier, so the remote probe had no repo to query: it found nothing because
    # it looked nowhere. Reporting that as GO is the null-reading failure -- the
    # verdict has to say the instrument was blind, not that the world was clean.
    if not scanned and gh_alive:
        print("UNDETERMINED%s — could not resolve repo for #%d; cannot determine "
              "overlap status (the remote probe never ran -- this is NOT a clean "
              "GO). Re-run from the target repo, or qualify --issue as "
              "owner/name#%d." % (tag, args.issue, args.issue))
        return PREBRANCH_UNDETERMINED
    # #2831 -- the same null-reading rule one leg over: gh answered (the issue
    # state came back) but a PR listing could not be read on EITHER transport, so
    # the linkage/file-overlap probe looked at nothing. A total outage (gh_alive
    # False) keeps the documented fail-open GO; a HOLD above still outranks this.
    if blind and gh_alive:
        print("UNDETERMINED%s — could not list %s PRs for %s#%d over GraphQL or REST; "
              "the linkage/file-overlap probe never ran (this is NOT a clean GO). "
              "Re-run once the GitHub API answers."
              % (tag, " and ".join(blind), scanned, args.issue))
        return PREBRANCH_UNDETERMINED
    # #3047 -- the same rule on the LOCAL side of the overlap leg: the PR listings
    # answered, but the derived target set was empty, so the open-PR file-overlap
    # intersected nothing. Gated on gh_alive like the two blocks above, so a total
    # outage keeps the documented fail-open GO; a HOLD above still outranks this.
    if empty_derived_base is not None and gh_alive:
        print("UNDETERMINED%s — the target set derived from the branch diff vs %s "
              "is EMPTY, so the open-PR file-overlap probe for %s#%d never ran "
              "(this is NOT a clean GO). Commit the work (the diff sees commits "
              "only) or pass --files, then re-run."
              % (tag, empty_derived_base, scanned, args.issue))
        return PREBRANCH_UNDETERMINED
    print("GO%s — no remote overlap for %s#%d%s"
          % (tag, scanned, args.issue,
             " (pre-PR re-scan clean)" if pre_pr else ""))
    return 0


def _advisory_tag(count):
    """The verdict-line qualifier for ``count`` merged-PR advisories printed
    above it (#2733): ``' (3 merged-PR advisories above — confirm the specific
    defect is still unfixed)'``, singular at 1, and '' at 0 so a genuinely clean
    scan keeps its bare verdict. Sits directly after the verdict word
    (``GO (…) — …``) so ``^GO`` / ``^HOLD`` anchors still match."""
    if not count:
        return ""
    return (" (%d merged-PR advisor%s above — confirm the specific defect is "
            "still unfixed)" % (count, "y" if count == 1 else "ies"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--issue", required=True,
                    help="the GH issue this run targets: a bare number (1609 -> "
                         "cwd repo) or a qualified cross-repo ref "
                         "(octo/notes#31). #1609: a bare number can only "
                         "mean the cwd repo, so a cross-repo target MUST qualify.")
    ap.add_argument("--gh", default="gh", help="gh binary (test seam)")
    ap.add_argument("--git", default="git", help="git binary (test seam, #1070)")
    gate = ap.add_mutually_exclusive_group()
    gate.add_argument("--prebranch", action="store_true",
                      help="immediately-before-branching gate (#687): fetch + "
                           "issue-state re-verify + issue→PR linkage + file-overlap + "
                           "LOCAL worktree/branch overlap advisory (#1070); "
                           "exit 3 = HOLD, 0 = GO")
    gate.add_argument("--pre-pr", dest="pre_pr", action="store_true",
                      help="immediately-before-PR gate (#2013): the SAME checks as "
                           "--prebranch, asked a second time after the build — the "
                           "branch→PR window --prebranch cannot see. Derives --files "
                           "from the branch diff when omitted; pairs with --since. "
                           "exit 3 = HOLD, 4 = UNDETERMINED (e.g. an EMPTY derived "
                           "target set: nothing committed, #3047), 0 = GO (treat "
                           "HOLD as advisory: never auto-abort a finished build)")
    ap.add_argument("--files", action="append", default=[],
                    help="target file(s) to overlap-check (repeatable or comma-"
                         "separated); used by --prebranch and --pre-pr. Omitted "
                         "under --pre-pr, it is derived from the branch diff.")
    ap.add_argument("--base", default="origin/main",
                    help="(--pre-pr) base ref the branch diff is taken against when "
                         "--files is omitted. Default origin/main; pass the repo's "
                         "real trunk on dev-trunk repos (some repos take PRs on dev).")
    ap.add_argument("--since", default=None,
                    help="(--pre-pr) ISO-8601 branch-point instant. A hit created "
                         "at/after it is labelled 'build-window' (the race --prebranch "
                         "cannot catch); earlier ones 'pre-existing' (a real "
                         "--prebranch miss). Omitted = no label, never a guess.")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip the git fetch in --prebranch (tests / offline)")
    ap.add_argument("--no-local", action="store_true",
                    help="skip the LOCAL worktree/branch overlap advisory (#1070)")
    ap.add_argument("--no-base-check", action="store_true",
                    help="(--pre-pr) skip the base-freshness advisory (#2219). "
                         "Offline runs already degrade to a loud UNDETERMINED "
                         "note; this suppresses the probe entirely.")
    ap.add_argument("--merged-window-min", type=int, default=0,
                    help="(--prebranch) minutes: a MERGED PR touching a target file "
                         "HOLDs only if it merged within this window. 0 (default) = "
                         "all merged overlaps are advisory (#833); set N>0 to opt-in "
                         "to a recency HOLD window (test seam / explicit override).")
    ap.add_argument("--merged-since", default=None,
                    help="(--prebranch) explicit ISO-8601 cutoff; merges at/after it "
                         "count as recent. Overrides --merged-window-min (test seam).")
    ap.add_argument("--skip-pr", type=int, default=0,
                    help="PR number to exclude from all linkage-match output (the "
                         "run's own PR). Applied before _matches() in both advisory "
                         "and --prebranch modes. 0 (default) = no-op.")
    args = ap.parse_args(argv)
    # #1609: split the --issue value into a number + optional cross-repo qualifier.
    try:
        args.issue, args.issue_repo = _parse_issue_ref(args.issue)
    except ValueError as exc:
        ap.error(str(exc))
    if args.prebranch or args.pre_pr:
        return _prebranch(args)
    try:
        match_repo = _match_repo(args.gh, args.issue_repo, args.git)
        open_prs = _gh_pr_list(args.gh, "open", 50, args.issue_repo, args.git) or []
        merged_prs = _gh_pr_list(args.gh, "merged", 30, args.issue_repo, args.git) or []
        if args.skip_pr:
            open_prs   = [p for p in open_prs   if p.get("number") != args.skip_pr]
            merged_prs = [p for p in merged_prs if p.get("number") != args.skip_pr]
        # De-dup by PR number (a PR can't be both, but guard anyway).
        seen = set()
        for line in _matches(open_prs + merged_prs, args.issue, match_repo):
            key = line.split(" ", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            print(line)
        # Sibling/non-closing-mention advisory (#1756): see _prebranch()'s twin block.
        sibling = _sibling_mentions(merged_prs, args.issue, match_repo)
        if sibling:
            print("note — merged PR(s) mention #%d without a closing keyword "
                  "(informational — read the PR body/AC-status before treating #%d "
                  "as buildable):" % (args.issue, args.issue))
            for s in sibling:
                print("  - " + s)
    except Exception:
        pass  # advisory: never fail the preflight
    return 0


if __name__ == "__main__":
    sys.exit(main())
