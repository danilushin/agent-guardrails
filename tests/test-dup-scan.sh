#!/usr/bin/env bash
# test-dup-scan.sh — TDD for dup_scan.py, the remote-duplicate advisory wired into
# the eng-flow preflight (#622). Proves: a body "Closes #N" and a bare title "(#N)"
# both flag a PR; a mere cross-reference ("see #N") does NOT; a missing issue and a
# gh failure both yield empty output with exit 0 (fail-open). Uses a FAKE gh
# injected via `--gh` (no network), matching test-reconcile-prefetch.sh.

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

# ── fake gh (portable Python; honours `pr list --state <s> --json ...`) ──────
# Open set:  700 body "Closes #622"           -> flags 622 (body keyword)
#            701 title "...(#612)"             -> flags 612 (bare title ref)
#            702 body "see #800 for context"  -> flags NOTHING for 800 (cross-ref)
# Merged set:900 body "Fixes #618"            -> flags 618 (merged)
# FAKE_MERGED_PARTOF=<n> -> merged PR910 "Part of #<n>"    (#1756 sibling note)
# FAKE_MERGED_MENTION=<n> -> merged PR912 "see #<n> ..."   (#1756 bare-mention note)
# FAKE_GH_FAIL=1 -> exit 1 (fail-open path).
GH_FAKE="$(mktemp).py"
cat > "$GH_FAKE" <<'PYEOF'
import json, os, sys
if os.environ.get("FAKE_GH_FAIL") == "1":
    sys.exit(1)
argv = sys.argv[1:]
state = "open"
if "--state" in argv:
    state = argv[argv.index("--state") + 1]
OPEN = [
    {"number": 700, "title": "feat: remote dup scan", "url": "u/700",
     "state": "OPEN", "body": "Closes #622\n\ndetails"},
    {"number": 701, "title": "feat(host-health): timer drift (#612)", "url": "u/701",
     "state": "OPEN", "body": "adds timer drift detection"},
    {"number": 702, "title": "chore: unrelated", "url": "u/702",
     "state": "OPEN", "body": "see #800 for background context"},
]
MERGED = [
    {"number": 900, "title": "fix(intel): null sentinel", "url": "u/900",
     "state": "MERGED", "body": "Fixes #618"},
]
partof = os.environ.get("FAKE_MERGED_PARTOF")
if partof:
    MERGED.append({"number": 910, "title": "feat: pole two (Part of #%s)" % partof,
                   "url": "u/910", "state": "MERGED",
                   "body": "Part of #%s\n\nsecond pole work." % partof})
mention = os.environ.get("FAKE_MERGED_MENTION")
if mention:
    MERGED.append({"number": 912, "title": "chore: unrelated", "url": "u/912",
                   "state": "MERGED",
                   "body": "see #%s for background context" % mention})
negclose = os.environ.get("FAKE_MERGED_NEGATED_CLOSE")
if negclose:
    MERGED.append({"number": 913, "title": "fix(eng-flow): gap 1 only", "url": "u/913",
                   "state": "MERGED",
                   "body": ("## What this does NOT close\n\n#%s stays **open**. "
                             "The rest of the AC is unrun." % negclose)})
qspan = os.environ.get("FAKE_MERGED_QUOTED_SPAN")
if qspan:
    MERGED.append({"number": 914, "title": "docs: grade the bet", "url": "u/914",
                   "state": "MERGED",
                   "body": ("its `Closes #%s` keyword left the PR history, so "
                            "it does not close it." % qspan)})
qfence = os.environ.get("FAKE_MERGED_QUOTED_FENCE")
if qfence:
    MERGED.append({"number": 915, "title": "docs: trailer example", "url": "u/915",
                   "state": "MERGED",
                   "body": ("The trailer looks like this:\r\n\r\n```\r\nCloses #%s\r\n"
                            "```\r\n\r\nNothing is closed here." % qfence)})
# #3385 -- title shapes. A merged PR's title cites #N in prose but closes ANOTHER
# issue (the real PR #3370 / #3107 title, verbatim).
tcite = os.environ.get("FAKE_MERGED_TITLE_CITE")
if tcite:
    MERGED.append({"number": 916, "url": "u/916", "state": "MERGED",
                   "title": ("fix(reco): a numbered Next field keeps its ref-less "
                             "lead item as the rec -- the re-point at the first live "
                             "ref made #%s (item 4) the rec over another epic's due item "
                             "1 (Closes #3366)" % tcite),
                   "body": "Closes #3366\n\nrec.py fix."})
# The title's "(Closes #N)" is the ONLY closer, and the body has none (the PR
# #3211/#3212/#3213 shape: the squash commit subject closed the issue).
tclose = os.environ.get("FAKE_MERGED_TITLE_CLOSES")
if tclose:
    MERGED.append({"number": 917, "url": "u/917", "state": "MERGED",
                   "title": ("fix(premerge_guard): a refused update-branch waits "
                             "for CI (Closes #%s)" % tclose),
                   "body": "## Defect\n\nNo closing keyword in this body."})
# A "(#N)" in mid-title is a citation, not the trailing squash suffix.
tmid = os.environ.get("FAKE_MERGED_TITLE_MIDPAREN")
if tmid:
    MERGED.append({"number": 918, "url": "u/918", "state": "MERGED",
                   "title": ("docs(fc-hotlist): the (#%s) streak is measured strict "
                             "and adjudicated (Closes #3366)" % tmid),
                   "body": "Closes #3366"})
# #3455 -- a merged PR whose title ends in the bare "(#N)" suffix, no ref in the body.
tsuffix = os.environ.get("FAKE_MERGED_TITLE_SUFFIX")
if tsuffix:
    MERGED.append({"number": 919, "url": "u/919", "state": "MERGED",
                   "title": "feat(host-health): timer drift (#%s)" % tsuffix,
                   "body": "adds timer drift detection"})
# The same suffix, with "Part of #N" in the body: a sibling, never a match.
tsibling = os.environ.get("FAKE_MERGED_SUFFIX_PARTOF")
if tsibling:
    MERGED.append({"number": 920, "url": "u/920", "state": "MERGED",
                   "title": "feat: pole three (#%s)" % tsibling,
                   "body": "Part of #%s\n\nthird pole work." % tsibling})
print(json.dumps(OPEN if state == "open" else MERGED if state == "merged" else []))
PYEOF
GH_FAKE_NATIVE="$(cygpath -w "$GH_FAKE" 2>/dev/null || echo "$GH_FAKE")"
trap 'rm -f "$GH_FAKE"' EXIT

# dup_scan runs a `.py` gh path under its own interpreter (the .py test seam), so
# pass the fake directly — no shell wrapper (unexecutable on Windows).
run() { "$PY" "$(cygpath -w "$DUP" 2>/dev/null || echo "$DUP")" --issue "$1" --gh "$GH_FAKE_NATIVE"; }

# (a) body "Closes #622" flags PR 700
out="$(run 622)"; rc=$?
if [ "$rc" = "0" ] && grep -q "#700" <<< "$out"; then ok "(a) body Closes #622 flags PR700"; else bad "(a) expected #700 flagged" "rc=$rc out=[$out]"; fi

# (b) bare title "(#612)" flags PR 701 even with no body keyword
out="$(run 612)"
if grep -q "#701" <<< "$out" && grep -qi "OPEN" <<< "$out"; then ok "(b) title (#612) flags PR701"; else bad "(b) expected #701 flagged" "out=[$out]"; fi

# (c) merged PR closing #618 is flagged
out="$(run 618)"
if grep -q "#900" <<< "$out" && grep -qi "MERGED" <<< "$out"; then ok "(c) merged Fixes #618 flags PR900"; else bad "(c) expected #900 [MERGED]" "out=[$out]"; fi

# (d) cross-reference "see #800" must NOT flag (no closing kw, not in title)
out="$(run 800)"
if [ -z "$out" ]; then ok "(d) cross-ref #800 not flagged"; else bad "(d) #800 should be empty" "out=[$out]"; fi

# (e) absent issue -> empty
out="$(run 999)"
if [ -z "$out" ]; then ok "(e) absent issue empty"; else bad "(e) #999 should be empty" "out=[$out]"; fi

# (f) gh failure -> empty output + exit 0 (fail-open)
out="$(FAKE_GH_FAIL=1 run 622)"; rc=$?
if [ "$rc" = "0" ] && [ -z "$out" ]; then ok "(f) gh failure fails open (empty, exit 0)"; else bad "(f) expected empty + rc0" "rc=$rc out=[$out]"; fi

# -- skip-pr tests (#919) --------------------------------------------------------
run_skip() { "$PY" "$(cygpath -w "$DUP" 2>/dev/null || echo "$DUP")" --issue "$1" --skip-pr "$2" --gh "$GH_FAKE_NATIVE"; }

# (g) --skip-pr: own PR suppressed in advisory mode — PR 700 closes #622; skip 700 -> empty
out="$(run_skip 622 700)"; rc=$?
if [ "$rc" = "0" ] && [ -z "$out" ]; then ok "(g) --skip-pr 700 suppresses PR700 from advisory output"; else bad "(g) expected empty advisory when skip-pr=700" "rc=$rc out=[$out]"; fi

# (h) --skip-pr on an unrelated number leaves other matches intact — skip 999, PR 700 still flagged
out="$(run_skip 622 999)"; rc=$?
if [ "$rc" = "0" ] && grep -q "#700" <<< "$out"; then ok "(h) --skip-pr 999 leaves PR700 visible (only own PR hidden)"; else bad "(h) expected #700 still flagged when skip-pr=999" "rc=$rc out=[$out]"; fi

# -- sibling / non-closing-mention informational note (#1756) -------------------
# A merged PR that mentions the issue WITHOUT a genuine closing keyword (a sibling
# "Part of #N" co-PR, or any other bare mention) is surfaced under a new
# informational note -- never as a bare match line -- so #996's sibling_re
# exemption keeps its HOLD-avoidance behavior while the caller still gets signal.

# (i) a "Part of #N" sibling pole (merged) surfaces under the note, not as a match
out="$(FAKE_MERGED_PARTOF=622 run 622)"
if grep -qi "without a closing keyword" <<< "$out" && grep -q "  - #910" <<< "$out" && ! grep -q "^#910" <<< "$out"; then
  ok "(i) #1756: 'Part of #N' merged sibling surfaces under the note, not as a match"
else
  bad "(i) expected note with #910, no bare #910 match line" "out=[$out]"
fi

# (j) a bare non-closing mention ("see #N", merged) surfaces under the note too --
#     previously silently dropped entirely (mirrors test (d)'s cross-ref shape, but
#     MERGED so it is in scope for the new leg).
out="$(FAKE_MERGED_MENTION=622 run 622)"
if grep -qi "without a closing keyword" <<< "$out" && grep -q "  - #912" <<< "$out" && ! grep -q "^#912" <<< "$out"; then
  ok "(j) #1756: bare merged mention ('see #N') surfaces under the note"
else
  bad "(j) expected note with #912" "out=[$out]"
fi

# (k) a genuine "Closes #N" PR (PR900, issue 618) appears ONLY in the matched-line
#     output, never duplicated into the note
out="$(run 618)"
if grep -q "^#900" <<< "$out" && ! grep -qi "without a closing keyword" <<< "$out"; then
  ok "(k) #1756: genuine closer (#900) is not duplicated into the note"
else
  bad "(k) expected #900 as a match line, no note block" "out=[$out]"
fi

# (l) --skip-pr on the sibling PR's own number suppresses it from the note
out="$(FAKE_MERGED_PARTOF=622 run_skip 622 910)"
if ! grep -q "910" <<< "$out"; then
  ok "(l) #1756: --skip-pr on the sibling PR's own number suppresses it from the note"
else
  bad "(l) expected #910 fully suppressed" "out=[$out]"
fi

# (mm) #1869: a "## What this does NOT close" heading followed by a blank line
#      and a bare "#N stays open" paragraph must NOT be treated as a genuine
#      closer -- \s+ used to span the blank line and false-match "close" + the
#      ref as if the NOT and the paragraph break weren't there (PR#1864 repro).
#      The bare #N mention still surfaces under the note (ref_re still matches),
#      just never as a bare match line.
out="$(FAKE_MERGED_NEGATED_CLOSE=823 run 823)"
if grep -qi "without a closing keyword" <<< "$out" && grep -q "  - #913" <<< "$out" && ! grep -q "^#913" <<< "$out"; then
  ok "(mm) #1869: negated 'NOT close' heading across a blank line is not a genuine closer"
else
  bad "(mm) expected #913 under the note, never a bare match line" "out=[$out]"
fi

# (nn) #2981: a closing keyword QUOTED in an inline code span is literal text, not a
#      closer (GitHub leaves it unlinked; premerge_guard already strips it, #2976).
#      It must surface under the note -- the body still mentions #N -- and never as
#      a bare match line. Before the fix: a false duplicate AND a dropped note.
out="$(FAKE_MERGED_QUOTED_SPAN=824 run 824)"
if grep -qi "without a closing keyword" <<< "$out" && grep -q "  - #914" <<< "$out" && ! grep -q "^#914" <<< "$out"; then
  ok "(nn) #2981: a keyword quoted in an inline code span is not a genuine closer"
else
  bad "(nn) expected #914 under the note, never a bare match line" "out=[$out]"
fi

# (oo) #2981: the same for a fenced block, in a CRLF body (real PR bodies are often CRLF)
out="$(FAKE_MERGED_QUOTED_FENCE=825 run 825)"
if grep -qi "without a closing keyword" <<< "$out" && grep -q "  - #915" <<< "$out" && ! grep -q "^#915" <<< "$out"; then
  ok "(oo) #2981: a keyword inside a fenced block (CRLF body) is not a genuine closer"
else
  bad "(oo) expected #915 under the note, never a bare match line" "out=[$out]"
fi

# -- title citation vs title closer (#3385) --------------------------------------
# A title #N counts only after a closing keyword, or as the trailing "(#N)" squash
# suffix that (b) pins. A prose citation is not a closer. It moves to the #1756
# note, the same advisory a body mention gets.

# (pp) the PR #3370 shape: the title cites #3107 in prose and closes #3366 -> no
#      match line for 3107, and the PR is listed under the note
out="$(FAKE_MERGED_TITLE_CITE=3107 run 3107)"
if ! grep -q "^#916" <<< "$out" && grep -qi "without a closing keyword" <<< "$out" \
   && grep -q "  - #916" <<< "$out"; then
  ok "(pp) #3385: a prose title citation (#3370 shape) is not a match, it is a note"
else
  bad "(pp) expected #916 under the note, never a bare match line" "out=[$out]"
fi

# (qq) positive control: the title's "(Closes #N)" is the only closer -> still a match
out="$(FAKE_MERGED_TITLE_CLOSES=3208 run 3208)"
if grep -q "^#917" <<< "$out" && grep -qi "MERGED" <<< "$out"; then
  ok "(qq) #3385: a title-only '(Closes #N)' still flags the PR"
else
  bad "(qq) expected a #917 match line" "out=[$out]"
fi

# (rr) "(#N)" in mid-title is not the trailing squash suffix -> no match line
out="$(FAKE_MERGED_TITLE_MIDPAREN=2916 run 2916)"
if ! grep -q "^#918" <<< "$out" && grep -q "  - #918" <<< "$out"; then
  ok "(rr) #3385: a mid-title '(#N)' is a citation, not the squash suffix"
else
  bad "(rr) expected #918 under the note, never a bare match line" "out=[$out]"
fi

# -- a match line is never repeated under the note (#3455) ---------------------
# The note says "without a closing keyword", so a PR that _matches() counts as a
# closer must not appear under it. It used to: only a body closer was skipped.

# (ss) the title-only "(Closes #N)" PR from (qq) prints once, as its match line
out="$(FAKE_MERGED_TITLE_CLOSES=3208 run 3208)"
if grep -q "^#917" <<< "$out" && ! grep -q "  - #917" <<< "$out"; then
  ok "(ss) #3455: a title-only '(Closes #N)' match is not repeated under the note"
else
  bad "(ss) expected a #917 match line and no #917 note line" "out=[$out]"
fi

# (tt) a merged title ending in the bare "(#N)" suffix prints once, as its match line
out="$(FAKE_MERGED_TITLE_SUFFIX=3209 run 3209)"
if grep -q "^#919" <<< "$out" && ! grep -q "  - #919" <<< "$out"; then
  ok "(tt) #3455: a merged '(#N)' suffix match is not repeated under the note"
else
  bad "(tt) expected a #919 match line and no #919 note line" "out=[$out]"
fi

# (uu) guard: the same suffix with "Part of #N" in the body is a sibling -> note only
out="$(FAKE_MERGED_SUFFIX_PARTOF=3210 run 3210)"
if ! grep -q "^#920" <<< "$out" && grep -q "  - #920" <<< "$out"; then
  ok "(uu) #3455: a '(#N)' suffix with 'Part of #N' stays in the note, never a match"
else
  bad "(uu) expected #920 under the note, never a bare match line" "out=[$out]"
fi

echo ""
echo "  dup-scan: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
