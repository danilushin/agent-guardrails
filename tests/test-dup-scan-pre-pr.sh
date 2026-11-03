#!/usr/bin/env bash
# test-dup-scan-pre-pr.sh — TDD for dup_scan.py's `--pre-pr` mode (#2013).
#
# `--prebranch` (#687) moved the duplicate scan from turn-start to
# immediately-before-branching. That closed turn-start->branch and left
# branch->PR open — the LARGER window, since it spans the whole build. On
# 2026-08-09 a sibling session opened PR #2009 on issue #2006 33s after this
# session's worktree existed; both prebranch scans were clean AND correct, and
# nothing asked again, so PR #2010 was opened 10m31s later against an already-open
# duplicate.
#
# The detection was never the problem: at PR-open time #2009 was OPEN with
# "Closes #2006" in its body, so the SAME checks re-run at that moment would have
# HELD. These cases pin that second ask, plus the two things that differ from
# --prebranch: --files derived from the branch diff (ground truth at PR time), and
# --since labelling build-window vs pre-existing hits.
#
# Uses a FAKE gh + FAKE git injected via --gh/--git (no network), mirroring
# test-dup-scan-prebranch.sh.
#
# Cases (p)-(z) cover the base-freshness advisory (#2219). Base freshness was
# checked at MERGE time and nowhere earlier, so a PR filed off a base that moved
# during the build was structurally undetectable until `mgo` -- by which point it
# had burned a CI run against the wrong base. Case (w) drives the git transport
# against a REAL, offline git repo rather than a stub, because a two-transport
# check whose second transport is only ever mocked is a single-transport check
# with extra ceremony (#2150 was filed for exactly that class of blindness).
#
# Cases (j) and (aa)-(ee) cover the EMPTY derived target set (#3047): `...HEAD`
# sees commits only, so uncommitted work (or a git failure) derived nothing, the
# open-PR file-overlap leg intersected nothing, and the scan read as a clean GO.
#
# Cases (gg)-(jj) cover the HOLD headline (#3413): a hold that is only a file
# overlap with another issue's PR no longer prints "#N is already covered".

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DUP="$SCRIPT_DIR/../scripts/dup_scan.py"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $1"; echo "        $2"; }

[ -f "$DUP" ] || { echo "FATAL: dup_scan.py not found at $DUP"; exit 1; }

# ── interpreter detection (mirror run-tests.sh) ─────────────────────────────
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for _c in python3 python; do
    for _e in $_c; do
      if [ -x "$_e" ] || command -v "$_e" >/dev/null 2>&1; then PY="$_e"; break 2; fi
    done
  done
fi
[ -n "$PY" ] || { echo "  FAIL  no python interpreter"; exit 1; }

# ── fake gh — the real #2006 collision, replayed.
#   issue state: FAKE_ISSUE_STATE (default OPEN — it closed at 05:36:26Z when
#                #2010 merged, 4m16s AFTER #2010 was opened).
#   FAKE_PR_CLOSES=<n> -> open PR 2009 with body "Closes #<n>" (the linkage hit).
#   FAKE_PR_CREATED=<iso> -> override 2009's createdAt (default 05:21:39Z, i.e.
#                33s after the 05:21:06Z branch point -> build-window).
#   FAKE_NO_CREATED=1  -> drop createdAt entirely (unlabelable -> no guess).
#   FAKE_GH_FAIL=1     -> exit 1 (fail-open path).
GH_FAKE="$(mktemp).py"
cat > "$GH_FAKE" <<'PYEOF'
import json, os, sys
if os.environ.get("FAKE_GH_FAIL") == "1":
    sys.exit(1)
argv = sys.argv[1:]
if argv[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner":
                      os.environ.get("FAKE_CWD_REPO", "octo/home-repo")}))
    sys.exit(0)
if argv[:2] == ["issue", "view"]:
    print(json.dumps({"state": os.environ.get("FAKE_ISSUE_STATE", "OPEN"),
                      "title": "markitdown probe disagrees with its runner",
                      "url": "u/issue"}))
    sys.exit(0)
if argv[:1] == ["api"] and "/compare/" in argv[1]:
    # #2219 base-freshness, transport A. Answers with a well-formed compare
    # object by default (behind_by 0 -> quiet), so cases (a)-(o) are untouched.
    #   FAKE_BEHIND=<n>          -> commits the base is ahead by
    #   FAKE_COMPARE_FAIL=1      -> non-zero exit (transport A blind)
    #   FAKE_EXPECT_COMPARE=<s>  -> refuse unless the ref spec contains <s>
    if os.environ.get("FAKE_COMPARE_FAIL") == "1":
        sys.exit(1)
    want = os.environ.get("FAKE_EXPECT_COMPARE")
    if want and want not in argv[1]:
        sys.exit(1)
    print(json.dumps({"status": "diverged",
                      "behind_by": int(os.environ.get("FAKE_BEHIND", "0")),
                      "ahead_by": 1}))
    sys.exit(0)
if argv[:2] == ["pr", "list"]:
    state = argv[argv.index("--state") + 1] if "--state" in argv else "open"
    # An unrelated open PR touching a/foo.py — the file-overlap fixture.
    OPEN = [
        {"number": 700, "title": "feat: unrelated", "url": "u/700", "state": "OPEN",
         "body": "no closing keyword here", "createdAt": "2026-08-01T00:00:00Z",
         "files": [{"path": "a/foo.py"}, {"path": "b/bar.py"}]},
    ]
    closes = os.environ.get("FAKE_PR_CLOSES")
    if closes:
        pr = {"number": 2009, "title": "fix: markitdown probe", "url": "u/2009",
              "state": "OPEN", "body": "Closes #%s" % closes,
              "files": [{"path": "skills/ingest/pdf_convert.py"}]}
        if os.environ.get("FAKE_NO_CREATED") != "1":
            pr["createdAt"] = os.environ.get("FAKE_PR_CREATED",
                                             "2026-08-09T05:21:39Z")
        OPEN.append(pr)
    if os.environ.get("FAKE_OVERLAP_3411") == "1":
        # #3413: the 2026-10-29 shape -- PR #3411 closes #3367 and shares only
        # deploy/mac/jobs.conf with #3410's branch (disjoint hunks, clean merge).
        OPEN.append({"number": 3411, "title": "fix(fc): retire the hotlist_status.json pulls",
                     "url": "u/3411", "state": "OPEN", "body": "Closes #3367",
                     "createdAt": "2026-10-29T19:00:00Z",
                     "files": [{"path": "deploy/mac/jobs.conf"}]})
    MERGED = []
    tcite = os.environ.get("FAKE_MERGED_TITLE_CITE")
    if tcite:  # #3385: the PR #3370 shape -- the title CITES #N, the PR closes another issue
        MERGED.append({"number": 3370, "url": "u/3370", "state": "MERGED",
                       "title": ("fix(reco): the re-point at the first live ref made "
                                 "#%s (item 4) the rec (Closes #3366)" % tcite),
                       "body": "Closes #3366", "mergedAt": "2026-10-27T10:00:00Z",
                       "createdAt": "2026-10-27T09:00:00Z",
                       "files": [{"path": "skills/reco/rec.py"}]})
    print(json.dumps(OPEN if state == "open" else MERGED if state == "merged" else []))
    sys.exit(0)
print("[]")
PYEOF
GH_FAKE_NATIVE="$(cygpath -w "$GH_FAKE" 2>/dev/null || echo "$GH_FAKE")"

# ── fake git — only `diff --name-only <base>...HEAD` matters for --pre-pr.
#   FAKE_DIFF_FILES (comma-sep) -> the branch's changed files.
#   FAKE_GIT_FAIL=1 -> exit 1 (derivation blind: UNDETERMINED, no crash, #3047).
#   Echoes the base ref it was asked for so a test can pin --base plumbing.
GIT_FAKE="$(mktemp).py"
cat > "$GIT_FAKE" <<'PYEOF'
import os, sys
if os.environ.get("FAKE_GIT_FAIL") == "1":
    sys.exit(1)
argv = sys.argv[1:]
if argv[:2] == ["diff", "--name-only"]:
    want = os.environ.get("FAKE_EXPECT_BASE")
    if want and argv[-1] != "%s...HEAD" % want:
        sys.exit(1)  # wrong base -> derivation yields nothing, test sees GO
    for f in os.environ.get("FAKE_DIFF_FILES", "").split(","):
        if f.strip():
            sys.stdout.write(f.strip() + "\n")
    sys.exit(0)
if argv[:2] == ["rev-parse", "--abbrev-ref"]:
    sys.stdout.write("fix/2006-markitdown-probe\n"); sys.exit(0)
if argv[:2] == ["worktree", "list"]:
    sys.stdout.write("worktree /repo/main\nHEAD aaaaaaa\n"
                     "branch refs/heads/fix/2006-markitdown-probe\n\n"); sys.exit(0)
if argv[:1] == ["for-each-ref"]:
    sys.stdout.write("fix/2006-markitdown-probe\n"); sys.exit(0)
if argv[:1] == ["rev-list"]:
    # #2219 transport C. The stub carries no commit graph, so it DECLINES here
    # rather than falling through to the catch-all exit 0 below: that would hand
    # back empty stdout, and case (u)'s null would then be an accident of the
    # stub's shape instead of a stated behaviour. Cases (w)/(y) drive this
    # transport against real git objects.
    sys.exit(1)
sys.exit(0)
PYEOF
GIT_FAKE_NATIVE="$(cygpath -w "$GIT_FAKE" 2>/dev/null || echo "$GIT_FAKE")"
trap 'rm -f "$GH_FAKE" "$GIT_FAKE"' EXIT

DUP_NATIVE="$(cygpath -w "$DUP" 2>/dev/null || echo "$DUP")"
# --no-fetch everywhere (tests must not touch a real remote); --no-local isolates
# these from the dev's real branches/worktrees (that advisory is #1070's, tested
# in test-dup-scan-prebranch.sh and unchanged here).
run() { "$PY" "$DUP_NATIVE" --pre-pr --no-fetch --no-local \
          --gh "$GH_FAKE_NATIVE" --git "$GIT_FAKE_NATIVE" "$@"; }

# The real branch point: session A's worktree, 2026-08-09T05:21:06Z.
BRANCH_POINT="2026-08-09T05:21:06Z"

# ── (a) THE REGRESSION — the #2006 timeline, replayed ────────────────────────
# At session A's pre-PR moment (05:32:10Z): issue #2006 still OPEN, PR #2009 OPEN
# with "Closes #2006". --prebranch could not have caught this (at 05:21:06Z #2009
# did not exist yet); --pre-pr must, with 10m31s to spare before #2010 merged.
out="$(FAKE_PR_CLOSES=2006 run --issue 2006)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "#2009" <<< "$out"; then
  ok "(a) #2006 replay: open PR closing the issue -> HOLD exit 3"
else bad "(a) expected HOLD rc3 naming #2009" "rc=$rc out=[$out]"; fi

# ── (b) clean pre-PR re-scan -> GO ───────────────────────────────────────────
out="$(run --issue 2006 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && grep -qi "pre-PR" <<< "$out"; then
  ok "(b) no collision -> GO exit 0, labelled pre-PR"
else bad "(b) expected GO rc0 + pre-PR label" "rc=$rc out=[$out]"; fi

# ── (c) --since labels a build-window hit ────────────────────────────────────
# #2009 created 05:21:39Z, 33s AFTER the branch point -> the race --prebranch
# structurally cannot catch. The tooling worked; the retro must not "fix" it.
out="$(FAKE_PR_CLOSES=2006 run --issue 2006 --since "$BRANCH_POINT")"; rc=$?
if [ "$rc" = "3" ] && grep -q "build-window" <<< "$out"; then
  ok "(c) hit created after the branch point -> labelled build-window"
else bad "(c) expected HOLD rc3 + 'build-window'" "rc=$rc out=[$out]"; fi

# ── (d) --since labels a pre-existing hit ────────────────────────────────────
# Same collision, but the PR predates the branch point -> a REAL --prebranch
# miss, diagnosed oppositely from (c).
out="$(FAKE_PR_CLOSES=2006 FAKE_PR_CREATED=2026-08-09T05:00:00Z \
        run --issue 2006 --since "$BRANCH_POINT")"; rc=$?
if [ "$rc" = "3" ] && grep -q "pre-existing" <<< "$out"; then
  ok "(d) hit created before the branch point -> labelled pre-existing"
else bad "(d) expected HOLD rc3 + 'pre-existing'" "rc=$rc out=[$out]"; fi

# ── (e) no --since -> no label (never a guess) ───────────────────────────────
out="$(FAKE_PR_CLOSES=2006 run --issue 2006)"; rc=$?
if [ "$rc" = "3" ] && ! grep -qE "build-window|pre-existing" <<< "$out"; then
  ok "(e) --since omitted -> HOLD without a fabricated window label"
else bad "(e) expected HOLD rc3 with NO window label" "rc=$rc out=[$out]"; fi

# ── (f) missing createdAt -> no label, still a HOLD ──────────────────────────
out="$(FAKE_PR_CLOSES=2006 FAKE_NO_CREATED=1 run --issue 2006 --since "$BRANCH_POINT")"; rc=$?
if [ "$rc" = "3" ] && ! grep -qE "build-window|pre-existing" <<< "$out"; then
  ok "(f) unlabelable hit (no createdAt) -> HOLD, no guessed label"
else bad "(f) expected HOLD rc3 with no label" "rc=$rc out=[$out]"; fi

# ── (g) --files DERIVED from the branch diff when omitted ────────────────────
# The scope-split limitation ("files emerge during execute") binds pre-branch
# only: at PR time the diff IS the target set. Open PR 700 touches a/foo.py.
out="$(FAKE_DIFF_FILES=a/foo.py run --issue 2006)"; rc=$?
if [ "$rc" = "3" ] && grep -q "#700" <<< "$out" && grep -qi "derived" <<< "$out"; then
  ok "(g) --files omitted -> derived from branch diff -> overlap HOLD"
else bad "(g) expected HOLD rc3 #700 + 'derived' note" "rc=$rc out=[$out]"; fi

# ── (h) explicit --files still wins over derivation ──────────────────────────
# The diff would collide (a/foo.py), but an explicit disjoint --files must be
# honoured verbatim — derivation is a fallback, not an override.
out="$(FAKE_DIFF_FILES=a/foo.py run --issue 2006 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && ! grep -qi "derived" <<< "$out"; then
  ok "(h) explicit --files overrides diff derivation -> GO"
else bad "(h) expected GO rc0, no derivation" "rc=$rc out=[$out]"; fi

# ── (i) --base is plumbed into the diff ──────────────────────────────────────
# Dev-trunk repos (some repos take PRs on dev) must not diff against main.
out="$(FAKE_DIFF_FILES=a/foo.py FAKE_EXPECT_BASE=origin/dev \
        run --issue 2006 --base origin/dev)"; rc=$?
if [ "$rc" = "3" ] && grep -q "#700" <<< "$out"; then
  ok "(i) --base origin/dev reaches the diff -> overlap HOLD"
else bad "(i) expected HOLD rc3 #700 with --base origin/dev" "rc=$rc out=[$out]"; fi

# ── (j) diff derivation blind on a git failure -> UNDETERMINED, never GO ─────
# git broken -> no derived targets -> the open-PR file-overlap leg had nothing to
# intersect. #3047: this used to assert GO exit 0, i.e. it pinned the defect -- an
# empty target set describes the INSTRUMENT, not the world. It must still not
# crash: the verdict is a stated UNDETERMINED (exit 4), not a traceback.
out="$(FAKE_GIT_FAIL=1 run --issue 2006 2>&1)"; rc=$?
if [ "$rc" = "4" ] && grep -q "target set EMPTY" <<< "$out" \
   && grep -q "file-overlap NOT checked" <<< "$out" \
   && grep -q "^UNDETERMINED" <<< "$out" \
   && ! grep -q "^GO" <<< "$out" && ! grep -q "Traceback" <<< "$out"; then
  ok "(j) git failure -> derivation blind -> UNDETERMINED exit 4, no crash"
else bad "(j) expected UNDETERMINED rc4 + NOT-checked note on git fail" "rc=$rc out=[$out]"; fi

# ── empty derived target set (#3047) ═════════════════════════════════════════

# ── (aa) THE DEFECT — nothing committed vs the base -> UNDETERMINED ──────────
# `<base>...HEAD` sees commits only, so a branch whose changes are still in the
# working tree derives an EMPTY set. That printed nothing and exited GO 0 -- the
# 2026-10-13 #3044 run read rc 0 before the commit and HOLD 3 after it.
out="$(run --issue 2006 2>&1)"; rc=$?
if [ "$rc" = "4" ] && grep -q "target set EMPTY" <<< "$out" \
   && grep -q "nothing committed vs origin/main" <<< "$out" \
   && grep -q "file-overlap NOT checked" <<< "$out" \
   && grep -q "^UNDETERMINED" <<< "$out" && ! grep -q "^GO" <<< "$out"; then
  ok "(aa) empty branch diff -> NOT-checked note + UNDETERMINED exit 4"
else bad "(aa) expected UNDETERMINED rc4 + NOT-checked note on an empty diff" "rc=$rc out=[$out]"; fi

# ── (bb) precedence: a HOLD from another leg still wins ──────────────────────
# HOLD (3) > UNDETERMINED (4): a positive finding outranks a blind leg. The note
# still prints -- the file-overlap leg was not checked, whatever else was found.
out="$(FAKE_PR_CLOSES=2006 run --issue 2006 2>&1)"; rc=$?
if [ "$rc" = "3" ] && grep -q "^HOLD" <<< "$out" \
   && grep -q "file-overlap NOT checked" <<< "$out" \
   && ! grep -q "^UNDETERMINED" <<< "$out"; then
  ok "(bb) empty diff + linkage HOLD -> HOLD exit 3 wins, note still printed"
else bad "(bb) expected HOLD rc3 over UNDETERMINED, with the note" "rc=$rc out=[$out]"; fi

# ── (cc) clean path: a NON-empty derived set keeps its note and its GO ───────
# The discriminating case for (aa): same inputs, one committed file that no open
# PR touches. A fix that went UNDETERMINED on every derived set would fail here.
out="$(FAKE_DIFF_FILES=c/other.py run --issue 2006 2>&1)"; rc=$?
if [ "$rc" = "0" ] && grep -q "derived from the branch diff vs origin/main (1 file(s))" <<< "$out" \
   && grep -q "^GO" <<< "$out" && ! grep -q "EMPTY" <<< "$out" \
   && ! grep -q "UNDETERMINED" <<< "$out"; then
  ok "(cc) non-empty derived set -> derived note + GO exit 0, no EMPTY note"
else bad "(cc) expected the derived note + GO rc0" "rc=$rc out=[$out]"; fi

# ── (dd) explicit --files is never a blind derivation ────────────────────────
# Derivation only runs when --files is omitted; an empty branch diff under an
# explicit target set must not read as EMPTY.
out="$(run --issue 2006 --files c/other.py 2>&1)"; rc=$?
if [ "$rc" = "0" ] && grep -q "^GO" <<< "$out" && ! grep -q "EMPTY" <<< "$out"; then
  ok "(dd) explicit --files + empty diff -> GO exit 0, no EMPTY note"
else bad "(dd) expected GO rc0 and no EMPTY note with explicit --files" "rc=$rc out=[$out]"; fi

# ── (ee) total gh outage keeps the documented fail-open GO ───────────────────
# The #2507 residual (module docstring): a total gh outage stays fail-open, and
# widening that is out of #3047's scope. The note still says the leg went
# unchecked, so the GO is not silent about it.
out="$(FAKE_GH_FAIL=1 run --issue 2006 2>&1)"; rc=$?
if [ "$rc" = "0" ] && grep -q "file-overlap NOT checked" <<< "$out" \
   && grep -q "^GO" <<< "$out"; then
  ok "(ee) total gh outage + empty diff -> fail-open GO exit 0, note printed"
else bad "(ee) expected fail-open GO rc0 with the NOT-checked note" "rc=$rc out=[$out]"; fi

# ── (k) gh fails open ────────────────────────────────────────────────────────
out="$(FAKE_GH_FAIL=1 FAKE_PR_CLOSES=2006 run --issue 2006 --files a/foo.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out"; then
  ok "(k) gh failure fails open -> GO exit 0"
else bad "(k) expected GO rc0 on gh fail" "rc=$rc out=[$out]"; fi

# ── (l) issue CLOSED mid-build -> HOLD ───────────────────────────────────────
# The other half of the #2006 race: #2010 merged at 05:36:25Z and closed the
# issue one second later. A session still building at that point must be told.
out="$(FAKE_ISSUE_STATE=CLOSED run --issue 2006 --files c/other.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "CLOSED" <<< "$out"; then
  ok "(l) issue closed during the build -> HOLD exit 3"
else bad "(l) expected HOLD rc3 CLOSED" "rc=$rc out=[$out]"; fi

# ── (m) the verdict text is pre-PR-appropriate ───────────────────────────────
# "do NOT branch" is dead advice once the build is paid for. The four live
# options must be named, and the HOLD marked advisory so no caller auto-aborts.
out="$(FAKE_PR_CLOSES=2006 run --issue 2006)"; rc=$?
if grep -qi "duplicate PR" <<< "$out" && grep -qi "rebase" <<< "$out" \
   && grep -qi "harvest" <<< "$out" && grep -qi "ADVISORY" <<< "$out" \
   && ! grep -qi "do NOT branch" <<< "$out"; then
  ok "(m) pre-PR verdict names the live options, not 'do NOT branch'"
else bad "(m) expected pre-PR wording + ADVISORY, no 'do NOT branch'" "rc=$rc out=[$out]"; fi

# ── (n) --prebranch and --pre-pr are mutually exclusive ──────────────────────
out="$("$PY" "$DUP_NATIVE" --prebranch --pre-pr --no-fetch --issue 2006 2>&1)"; rc=$?
if [ "$rc" = "2" ]; then
  ok "(n) --prebranch + --pre-pr -> argparse error exit 2"
else bad "(n) expected exit 2 on both gates" "rc=$rc out=[$out]"; fi

# ── (o) --prebranch is unchanged (regression guard) ──────────────────────────
# The pre-branch verdict must keep its own wording and must NOT derive files.
out="$(FAKE_DIFF_FILES=a/foo.py "$PY" "$DUP_NATIVE" --prebranch --no-fetch --no-local \
        --gh "$GH_FAKE_NATIVE" --git "$GIT_FAKE_NATIVE" --issue 2006 2>&1)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && ! grep -qi "derived" <<< "$out"; then
  ok "(o) --prebranch unchanged: no diff derivation, own wording"
else bad "(o) --prebranch regressed" "rc=$rc out=[$out]"; fi

# ── base-freshness advisory (#2219) ══════════════════════════════════════════

# ── (p) THE NON-NULL — a branch genuinely behind its base is reported ────────
# The zero-metric gate: a check that only ever stays silent would pass every
# "no false positive" test while measuring nothing. This is the case that proves
# the instrument moves. #2218's real shape: 2 commits landed during the build.
out="$(FAKE_BEHIND=2 run --issue 2006 --files c/other.py 2>&1)"; rc=$?
if [ "$rc" = "0" ] && grep -q "2 commit(s) behind" <<< "$out"; then
  ok "(p) base moved during the build -> advisory names the behind-count"
else bad "(p) expected '2 commit(s) behind' advisory at rc0" "rc=$rc out=[$out]"; fi

# ── (q) the advisory carries BOTH remedies, correctly scoped ─────────────────
# #2219's DoD asks for the `update-branch` line. At --pre-pr time no PR exists
# yet, so that line has no number to name: the actionable pre-file fix is local,
# and `update-branch` is its post-filing equivalent. Both must appear, labelled.
out="$(FAKE_BEHIND=2 run --issue 2006 --files c/other.py 2>&1)"; rc=$?
if grep -q "git merge origin/main" <<< "$out" \
   && grep -q "update-branch" <<< "$out" \
   && grep -qi "advisory" <<< "$out"; then
  ok "(q) advisory names the local fix AND the post-filing update-branch line"
else bad "(q) expected both remedies + ADVISORY marker" "rc=$rc out=[$out]"; fi

# ── (r) fresh base -> silent (quiet-when-clean, the other half of non-vacuity) ─
out="$(FAKE_BEHIND=0 run --issue 2006 --files c/other.py 2>&1)"; rc=$?
if [ "$rc" = "0" ] && ! grep -qi "behind" <<< "$out"; then
  ok "(r) base fresh -> no advisory (quiet when clean)"
else bad "(r) expected silence on a fresh base" "rc=$rc out=[$out]"; fi

# ── (s) advisory NEVER touches the exit code ─────────────────────────────────
# Advisory means advisory: a stale base alone must not turn GO into HOLD, and a
# real HOLD must keep exit 3 rather than being masked by the new code path.
out="$(FAKE_BEHIND=5 FAKE_PR_CLOSES=2006 run --issue 2006 2>&1)"; rc=$?
if [ "$rc" = "3" ] && grep -q "5 commit(s) behind" <<< "$out" \
   && grep -qi "HOLD" <<< "$out"; then
  ok "(s) stale base + real HOLD -> still exit 3, both surfaced"
else bad "(s) expected rc3 with both the HOLD and the advisory" "rc=$rc out=[$out]"; fi

# ── (t) --base origin/main is normalized to the remote BRANCH name ───────────
# The compare API and the git refspec both take a branch, not a remote-tracking
# ref: asking for `origin/main...HEAD` 404s, which would fail open and read as
# "fresh" -- a silent false negative in exactly the case the check exists for.
out="$(FAKE_BEHIND=3 FAKE_EXPECT_COMPARE="compare/main...fix/2006" \
        run --issue 2006 --files c/other.py --base origin/main 2>&1)"; rc=$?
if grep -q "3 commit(s) behind" <<< "$out"; then
  ok "(t) --base origin/main reaches the compare API as 'main'"
else bad "(t) expected the compare probe to strip the origin/ prefix" "rc=$rc out=[$out]"; fi

# ── (u) ALL THREE transports blind -> LOUD undetermined, never silence ───────
# A null reading measures the instrument first: with compare failing, the remote
# git transport declining (local origin is not the named repo) and the local one
# declining too (the git stub has no commit graph and says so), the honest output
# is "could not measure", not the fresh-base silence of case (r).
out="$(FAKE_COMPARE_FAIL=1 FAKE_CWD_REPO=someone/else \
        run --issue 2006 --files c/other.py 2>&1)"; rc=$?
if [ "$rc" = "0" ] && grep -qi "could not measure" <<< "$out"; then
  ok "(u) all three transports blind -> undetermined aloud, exit unchanged"
else bad "(u) expected a loud 'could not measure' note at rc0" "rc=$rc out=[$out]"; fi

# ── (v) --no-base-check opts out ─────────────────────────────────────────────
out="$(FAKE_BEHIND=9 run --issue 2006 --files c/other.py --no-base-check 2>&1)"; rc=$?
if [ "$rc" = "0" ] && ! grep -qi "behind" <<< "$out"; then
  ok "(v) --no-base-check suppresses the probe entirely"
else bad "(v) expected no base output under --no-base-check" "rc=$rc out=[$out]"; fi

# ── (w) transport B against a REAL git repo, fully offline ───────────────────
# #2150 exists because the compare endpoint 404'd for ~4.5h and reported a
# 0-behind PR four times. The git fallback is what makes this check survive
# that, so it is driven here against real git objects over a local-path remote:
# a bare repo laid out as <tmp>/octo/home-repo.git so the owner/repo
# parse premerge_guard.behind_via_git performs on the origin URL succeeds.
GT="$(mktemp -d)"
UP="$GT/octo/home-repo.git"
WORK="$GT/work"
GC=(-c user.email=t@example.com -c user.name=t -c commit.gpgsign=false)
mkdir -p "$(dirname "$UP")"
if git init -q --bare "$UP" 2>/dev/null \
   && git init -q -b main "$WORK" 2>/dev/null \
   && git -C "$WORK" remote add origin "$UP" \
   && git -C "$WORK" "${GC[@]}" commit -q --allow-empty -m base \
   && git -C "$WORK" push -q origin main \
   && git -C "$WORK" checkout -q -b fix/2219-stale \
   && git -C "$WORK" "${GC[@]}" commit -q --allow-empty -m work \
   && git -C "$WORK" push -q origin fix/2219-stale \
   && git -C "$WORK" checkout -q main \
   && git -C "$WORK" "${GC[@]}" commit -q --allow-empty -m advance-1 \
   && git -C "$WORK" "${GC[@]}" commit -q --allow-empty -m advance-2 \
   && git -C "$WORK" push -q origin main \
   && git -C "$WORK" checkout -q fix/2219-stale; then
  out="$(cd "$WORK" && FAKE_COMPARE_FAIL=1 "$PY" "$DUP_NATIVE" --pre-pr --no-fetch \
          --no-local --gh "$GH_FAKE_NATIVE" --issue 2006 --files c/other.py \
          --base origin/main 2>&1)"; rc=$?
  if [ "$rc" = "0" ] && grep -q "2 commit(s) behind" <<< "$out" \
     && grep -qi "git" <<< "$out"; then
    ok "(w) compare API blind -> real git transport measures 2 behind"
  else bad "(w) expected the git fallback to report 2 behind" "rc=$rc out=[$out]"; fi
else
  bad "(w) real-git fixture setup failed" "could not build $UP"
fi
rm -rf "$GT" 2>/dev/null

# ── (y) THE RUNNING CONFIGURATION: an UN-PUSHED branch, measured locally ─────
# This is the case --pre-pr actually runs in. Both remote transports resolve the
# HEAD ref on origin, and at pre-PR time the branch normally has never been
# pushed -- so both correctly decline, and a two-transport check would print
# UNDETERMINED on the majority of the runs it exists for. Reachable is not the
# same as effective: that check would run every time and answer almost never.
# Fixture: same offline layout as (w), except fix/2219-local is NEVER pushed.
GT2="$(mktemp -d)"
UP2="$GT2/octo/home-repo.git"
WORK2="$GT2/work"
mkdir -p "$(dirname "$UP2")"
if git init -q --bare "$UP2" 2>/dev/null \
   && git init -q -b main "$WORK2" 2>/dev/null \
   && git -C "$WORK2" remote add origin "$UP2" \
   && git -C "$WORK2" "${GC[@]}" commit -q --allow-empty -m base \
   && git -C "$WORK2" push -q origin main \
   && git -C "$WORK2" checkout -q -b fix/2219-local \
   && git -C "$WORK2" "${GC[@]}" commit -q --allow-empty -m work \
   && git -C "$WORK2" checkout -q main \
   && git -C "$WORK2" "${GC[@]}" commit -q --allow-empty -m advance-1 \
   && git -C "$WORK2" "${GC[@]}" commit -q --allow-empty -m advance-2 \
   && git -C "$WORK2" "${GC[@]}" commit -q --allow-empty -m advance-3 \
   && git -C "$WORK2" push -q origin main \
   && git -C "$WORK2" checkout -q fix/2219-local; then
  # Belt-and-braces: prove the head really is absent from origin, so this case
  # cannot silently degrade into a re-run of (w) if a push crept in above.
  if git -C "$WORK2" ls-remote --heads origin fix/2219-local | grep -q .; then
    bad "(y) fixture invalid" "fix/2219-local unexpectedly exists on origin"
  else
    out="$(cd "$WORK2" && FAKE_COMPARE_FAIL=1 "$PY" "$DUP_NATIVE" --pre-pr \
            --no-fetch --no-local --gh "$GH_FAKE_NATIVE" --issue 2006 \
            --files c/other.py --base origin/main 2>&1)"; rc=$?
    if [ "$rc" = "0" ] && grep -q "3 commit(s) behind" <<< "$out" \
       && grep -q "local git" <<< "$out"; then
      ok "(y) un-pushed branch: local transport measures 3 behind"
    else bad "(y) expected the local transport to report 3 behind" "rc=$rc out=[$out]"; fi
  fi
else
  bad "(y) un-pushed fixture setup failed" "could not build $UP2"
fi
rm -rf "$GT2" 2>/dev/null

# ── (z) the local transport is a FALLBACK, not a silent override ─────────────
# It must not fire when a remote transport already answered: a local count and a
# compare count can disagree (unpushed commits), and the advisory names which
# transport produced the number. Compare says 2 -> that is the number reported.
out="$(FAKE_BEHIND=2 run --issue 2006 --files c/other.py 2>&1)"; rc=$?
if [ "$rc" = "0" ] && grep -q "2 commit(s) behind" <<< "$out" \
   && grep -q "compare API" <<< "$out" && ! grep -q "local git" <<< "$out"; then
  ok "(z) compare answered -> local transport not consulted"
else bad "(z) expected the compare answer to win" "rc=$rc out=[$out]"; fi

# ── (x) --prebranch does NOT run the base check (scope guard) ────────────────
# Pre-branch is BEFORE any commits exist on the branch, so "behind" is not yet a
# meaningful question there; widening the gate silently would also change the
# eng-flow preflight's cost, which is not what #2219 asked for.
out="$(FAKE_BEHIND=4 "$PY" "$DUP_NATIVE" --prebranch --no-fetch --no-local \
        --gh "$GH_FAKE_NATIVE" --git "$GIT_FAKE_NATIVE" --issue 2006 2>&1)"; rc=$?
if [ "$rc" = "0" ] && ! grep -qi "behind" <<< "$out"; then
  ok "(x) --prebranch unchanged: no base-freshness probe"
else bad "(x) --prebranch must not run the base check" "rc=$rc out=[$out]"; fi

# ── (ff) #3385: a prose title citation is not a pre-PR HOLD ─────────────────
# `--pre-pr --issue 3107` exited 3 because PR #3370's title cited #3107 in prose
# while closing #3366. It is GO, and the PR is listed under the #1756 note.
out="$(FAKE_MERGED_TITLE_CITE=3107 run --issue 3107 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "^GO" <<< "$out" && ! grep -qi "HOLD" <<< "$out" \
   && grep -q "  - #3370" <<< "$out"; then
  ok "(ff) #3385: the #3370 prose citation -> GO exit 0 + note naming #3370"
else bad "(ff) expected GO rc0, no HOLD, note with #3370" "rc=$rc out=[$out]"; fi

# ── #3413: the headline says what the holds found ═══════════════════════════

# ── (gg) THE DEFECT — a file overlap alone is not "already covered" ──────────
# `--pre-pr --issue 3410` held on PR #3411's deploy/mac/jobs.conf and printed
# "#3410 is already covered", but #3411 closes #3367. The exit code stays 3 and
# the reason still lists the shared file.
out="$(FAKE_OVERLAP_3411=1 run --issue 3410 --files deploy/mac/jobs.conf)"; rc=$?
hold_line="$(printf '%s\n' "$out" | grep '^HOLD')"
if [ "$rc" = "3" ] && [[ "$hold_line" == "HOLD — file overlap only (PR #3411), not a link to octo/home-repo#3410: check the hunks before filing:" ]] \
   && grep -q "OPEN PR #3411 changed files intersect the target set: deploy/mac/jobs.conf" <<< "$out" \
   && ! grep -q "already covered" <<< "$out"; then
  ok "(gg) #3413: overlap-only HOLD names the PR, not 'already covered'; exit 3"
else bad "(gg) expected the overlap-only HOLD opener at rc3" "rc=$rc hold_line=[$hold_line] out=[$out]"; fi

# ── (hh) a link hold keeps "already covered" ─────────────────────────────────
out="$(FAKE_PR_CLOSES=2006 run --issue 2006 --files c/other.py)"; rc=$?
hold_line="$(printf '%s\n' "$out" | grep '^HOLD')"
if [ "$rc" = "3" ] && [[ "$hold_line" == "HOLD — octo/home-repo#2006 is already covered; do NOT open a duplicate PR:" ]]; then
  ok "(hh) #3413: a PR closing #N still reads 'already covered'"
else bad "(hh) expected the 'already covered' opener at rc3" "rc=$rc hold_line=[$hold_line] out=[$out]"; fi

# ── (ii) a link beside an overlap still reads "already covered" ──────────────
# PR #2009 closes #2006 and PR #700 shares a/foo.py: one link is enough.
out="$(FAKE_PR_CLOSES=2006 run --issue 2006 --files a/foo.py)"; rc=$?
hold_line="$(printf '%s\n' "$out" | grep '^HOLD')"
if [ "$rc" = "3" ] && grep -q "already covered" <<< "$hold_line" \
   && ! grep -q "file overlap only" <<< "$hold_line"; then
  ok "(ii) #3413: link + overlap -> 'already covered'"
else bad "(ii) expected 'already covered' when a link hold is present" "rc=$rc hold_line=[$hold_line] out=[$out]"; fi

# ── (jj) a CLOSED issue beside an overlap still reads "already covered" ──────
out="$(FAKE_ISSUE_STATE=CLOSED run --issue 2006 --files a/foo.py)"; rc=$?
hold_line="$(printf '%s\n' "$out" | grep '^HOLD')"
if [ "$rc" = "3" ] && grep -q "already covered" <<< "$hold_line" \
   && ! grep -q "file overlap only" <<< "$hold_line"; then
  ok "(jj) #3413: closed issue + overlap -> 'already covered'"
else bad "(jj) expected 'already covered' when the issue is CLOSED" "rc=$rc hold_line=[$hold_line] out=[$out]"; fi

echo ""
echo "  dup-scan-pre-pr: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
