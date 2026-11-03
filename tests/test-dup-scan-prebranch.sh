#!/usr/bin/env bash
# test-dup-scan-prebranch.sh — TDD for dup_scan.py's `--prebranch` mode (#687).
# The solo-inline gogogo path has no enforced preflight: it scans at turn-start,
# then builds for minutes, so a concurrent session can file+merge a duplicate in
# the build window. `--prebranch` bundles the three immediately-before-branching
# checks into one HOLD/GO callable: (1) git fetch (skippable via --no-fetch in
# tests), (2) issue-state re-verify (CLOSED ⇒ HOLD), (3) issue→PR linkage AND
# open-PR file-overlap vs the target set (--files) ⇒ HOLD. Exit 3 = HOLD, 0 = GO.
# Infra failure fails OPEN (exit 0) like the default advisory mode. Uses a FAKE gh
# injected via --gh (no network), mirroring test-dup-scan.sh.

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

# ── fake gh — honours `issue view <n> --json …` and `pr list --state <s> --json …`
#   issue state: env FAKE_ISSUE_STATE (default OPEN).
#   open PR 700 touches a/foo.py + b/bar.py, body unrelated (no linkage).
#   FAKE_PR_CLOSES=<n> adds open PR 701 with body "Closes #<n>" (linkage hit).
#   FAKE_GH_FAIL=1 -> exit 1 (fail-open path).
GH_FAKE="$(mktemp).py"
cat > "$GH_FAKE" <<'PYEOF'
import json, os, re, sys
if os.environ.get("FAKE_GH_FAIL") == "1":
    sys.exit(1)
argv = sys.argv[1:]
# #2827: a GraphQL-only limit. FAKE_GQL_FAIL=1 fails every GraphQL subcommand
# (issue/pr/repo) while `gh api` (REST) keeps answering; FAKE_REST_FAIL=1 fails
# REST too. The REST issue payload is written as raw UTF-8 with non-ASCII in the
# body -- REST returns the whole issue, and a locale-decoded read of it must not
# be what silently fails the fallback on Windows.
if os.environ.get("FAKE_GQL_FAIL") == "1" and argv[:1] in (["issue"], ["pr"], ["repo"]):
    sys.stderr.write("GraphQL: API rate limit already exceeded\n")
    sys.exit(1)
if argv[:1] == ["api"]:
    if os.environ.get("FAKE_REST_FAIL") == "1":
        sys.exit(1)
    _m = re.match(r"repos/([^/]+/[^/]+)/issues/(\d+)$", argv[1] if len(argv) > 1 else "")
    if _m:
        _payload = {"number": int(_m.group(2)),
                    "state": os.environ.get("FAKE_REST_STATE", "open"),
                    "title": "the target — via REST",
                    "html_url": "https://github.com/%s/issues/%s" % (_m.group(1), _m.group(2)),
                    "body": "café ✅ — שלום"}
        if os.environ.get("FAKE_REST_IS_PR") == "1":
            _payload["pull_request"] = {"url": "u/pr"}
        sys.stdout.buffer.write(json.dumps(_payload, ensure_ascii=False).encode("utf-8"))
        sys.exit(0)
    # anything else under `api` -- the #2831 /pulls twins -- is answered below,
    # after _prs() is defined, from the SAME fixtures `pr list` serves.
# repo view (#1609): the cwd repo's nameWithOwner, used to decide same- vs
# cross-repo issue matching. FAKE_CWD_REPO_FAIL=1 -> exit 1 (undeterminable).
if argv[:2] == ["repo", "view"]:
    if os.environ.get("FAKE_CWD_REPO_FAIL") == "1":
        sys.exit(1)
    print(json.dumps({"nameWithOwner":
                      os.environ.get("FAKE_CWD_REPO", "octo/home-repo")}))
    sys.exit(0)
if argv[:2] == ["issue", "view"]:
    # #1609: a qualified --issue passes --repo <owner/name>; when it names the
    # FOREIGN repo, return FAKE_FOREIGN_ISSUE_STATE so a test can make the target
    # (e.g. notes#31) differ from the cwd repo's own #N.
    _repo = argv[argv.index("--repo") + 1] if "--repo" in argv else None
    _foreign = os.environ.get("FAKE_FOREIGN_REPO")
    if _repo and _foreign and _repo == _foreign:
        _state = os.environ.get("FAKE_FOREIGN_ISSUE_STATE", "OPEN")
    else:
        _state = os.environ.get("FAKE_ISSUE_STATE", "OPEN")
    print(json.dumps({"state": _state, "title": "the target", "url": "u/issue"}))
    sys.exit(0)
def _prs(state, _prepo):
    # #2502: `pr list` is repo-scoped exactly like `issue view` already was. When
    # the scan qualifies the FOREIGN repo, it must see that repo's PRs -- not the
    # cwd repo's. Real `gh` resolves the repo from the process cwd when --repo is
    # absent, so "no --repo" here models the cwd repo, and the split below is what
    # a wrong-repo scan actually looks like.
    _pforeign = os.environ.get("FAKE_FOREIGN_REPO")
    if _prepo and _pforeign and _prepo == _pforeign:
        FOREIGN = []
        _ffiles = os.environ.get("FAKE_FOREIGN_PR_FILES")
        if _ffiles and state == "open":
            FOREIGN.append({"number": 770, "title": "feat: foreign in-flight",
                            "url": "u/770", "state": "OPEN",
                            "body": "no closing keyword here",
                            "files": [{"path": p} for p in _ffiles.split(",")]})
        _fqclose = os.environ.get("FAKE_PR_CLOSES_QUALIFIED")
        if _fqclose and state == "open":
            FOREIGN.append({"number": 702, "title": "feat: cross-repo dup", "url": "u/702",
                            "state": "OPEN", "body": "Closes %s" % _fqclose,
                            "files": [{"path": "z/cross.py"}]})
        return FOREIGN
    OPEN = [
        {"number": 700, "title": "feat: unrelated", "url": "u/700", "state": "OPEN",
         "body": "no closing keyword here",
         "files": [{"path": "a/foo.py"}, {"path": "b/bar.py"}]},
    ]
    closes = os.environ.get("FAKE_PR_CLOSES")
    if closes:
        OPEN.append({"number": 701, "title": "feat: dup", "url": "u/701", "state": "OPEN",
                     "body": "Closes #%s" % closes, "files": [{"path": "z/zzz.py"}]})
    MERGED = []
    mclose = os.environ.get("FAKE_MERGED_CLOSES")
    if mclose:  # a merged PR closing the issue (squash body carries Closes #N)
        MERGED.append({"number": 900, "title": "feat: shipped", "url": "u/900",
                       "state": "MERGED", "body": "Closes #%s" % mclose,
                       "mergedAt": "2026-06-16T13:00:00Z",
                       "files": [{"path": "m/merged.py"}]})
    # Merged fixtures touch files DISJOINT from the open PR (a/foo.py, b/bar.py) so
    # the merged-tiering tests isolate the merged path — an open-PR overlap can't
    # mask a broken merged branch (feedback-fixture-must-isolate-match-path).
    if os.environ.get("FAKE_MERGED_OVERLAP") == "1":  # merged RECENTLY, NO closing kw (#678)
        MERGED.append({"number": 901, "title": "feat: parallel", "url": "u/901",
                       "state": "MERGED", "body": "unrelated body, no closing keyword",
                       "mergedAt": "2026-06-16T13:30:00Z",
                       "files": [{"path": "m/recent.py"}]})
    if os.environ.get("FAKE_MERGED_OLD") == "1":  # merged BEFORE the window, NO closing kw (#699 advisory)
        MERGED.append({"number": 902, "title": "feat: old history", "url": "u/902",
                       "state": "MERGED", "body": "unrelated body, no closing keyword",
                       "mergedAt": "2026-06-16T08:00:00Z",
                       "files": [{"path": "m/old.py"}]})
    if os.environ.get("FAKE_MERGED_HOTFILE") == "1":  # the #682 case: many old merges to a hot file
        # Listed in a deliberately NON-monotonic mergedAt order (oldest, newest,
        # middle) so the newest-first advisory sort (#2733, case zz) cannot pass
        # by echoing the API order -- and a reverse sort cannot pass either.
        for _n, _ts in ((905, "2026-06-10T10:00:00Z"), (903, "2026-06-15T10:00:00Z"),
                        (904, "2026-06-14T10:00:00Z")):
            MERGED.append({"number": _n, "title": "docs: hot file edit", "url": "u/%d" % _n,
                           "state": "MERGED", "body": "unrelated, no closing keyword",
                           "mergedAt": _ts, "files": [{"path": "skills/curator/SKILL.md"}]})
    if os.environ.get("FAKE_MERGED_NODATE") == "1":  # merged overlap w/ NO mergedAt -> conservative HOLD
        MERGED.append({"number": 906, "title": "feat: undated", "url": "u/906",
                       "state": "MERGED", "body": "unrelated, no closing keyword",
                       "files": [{"path": "m/undated.py"}]})
    partof = os.environ.get("FAKE_MERGED_PARTOF")
    if partof:  # #996: a sibling pole of a multi-PR epic — "Part of #N" in title+body, NOT a closer (the #994 shape)
        MERGED.append({"number": 910, "title": "feat: pole two (Part of #%s)" % partof, "url": "u/910",
                       "state": "MERGED", "body": "Part of #%s\n\nsecond pole work." % partof,
                       "mergedAt": "2026-06-16T13:30:00Z",
                       "files": [{"path": "m/pole.py"}]})
    refs = os.environ.get("FAKE_MERGED_REFS")
    if refs:  # #996: sibling via "Refs #N" in body, plain title (no #N anywhere else)
        MERGED.append({"number": 911, "title": "feat: pole three", "url": "u/911",
                       "state": "MERGED", "body": "Refs #%s" % refs,
                       "mergedAt": "2026-06-16T13:31:00Z",
                       "files": [{"path": "m/pole3.py"}]})
    tcite = os.environ.get("FAKE_MERGED_TITLE_CITE")
    if tcite:  # #3385: the PR #3370 shape -- the title CITES #N, the PR closes another issue
        MERGED.append({"number": 916, "url": "u/916", "state": "MERGED",
                       "title": ("fix(reco): the re-point at the first live ref made "
                                 "#%s (item 4) the rec (Closes #3366)" % tcite),
                       "body": "Closes #3366", "mergedAt": "2026-06-16T13:32:00Z",
                       "files": [{"path": "m/rec.py"}]})
    tclose = os.environ.get("FAKE_MERGED_TITLE_CLOSES")
    if tclose:  # #3385: the title's "(Closes #N)" is the ONLY closer (the #3211 shape)
        MERGED.append({"number": 917, "url": "u/917", "state": "MERGED",
                       "title": "fix(premerge_guard): waits for CI (Closes #%s)" % tclose,
                       "body": "No closing keyword in this body.",
                       "mergedAt": "2026-06-16T13:33:00Z",
                       "files": [{"path": "m/guard.py"}]})
    return OPEN if state == "open" else MERGED if state == "merged" else []
if argv[:2] == ["pr", "list"]:
    _state = argv[argv.index("--state") + 1] if "--state" in argv else "open"
    _prepo = argv[argv.index("--repo") + 1] if "--repo" in argv else None
    print(json.dumps(_prs(_state, _prepo)))
    sys.exit(0)
# #2831: REST twins of `pr list`, served from the SAME _prs() fixtures in REST's
# shape (html_url, lowercase state, created_at/merged_at, files as `filename`).
# The nwo always arrives explicit; it is the cwd repo unless it is the foreign one.
# FAKE_REST_PULLS_FAIL=1 fails /pulls only (the issue read above still answers);
# FAKE_REST_FILES_FAIL=1 fails /pulls/<n>/files only; FAKE_REST_CLOSED_UNMERGED=<n>
# adds a CLOSED-but-never-merged PR "Closes #<n>" -- REST ?state=closed returns
# it, `pr list --state merged` never would.
if argv[:1] == ["api"]:
    _path = argv[1] if len(argv) > 1 else ""
    _fm = re.match(r"repos/([^/]+/[^/]+)/pulls/(\d+)/files(?:\?.*)?$", _path)
    _lm = re.match(r"repos/([^/]+/[^/]+)/pulls\?(.*)$", _path)
    if os.environ.get("FAKE_REST_PULLS_FAIL") == "1" and (_fm or _lm):
        sys.exit(1)
    if _fm:
        if os.environ.get("FAKE_REST_FILES_FAIL") == "1":
            sys.exit(1)
        for _p in _prs("open", _fm.group(1)) + _prs("merged", _fm.group(1)):
            if _p["number"] == int(_fm.group(2)):
                print(json.dumps([{"filename": f["path"]} for f in _p.get("files") or []]))
                sys.exit(0)
        print("[]")
        sys.exit(0)
    if _lm:
        _q = dict(kv.split("=", 1) for kv in _lm.group(2).split("&") if "=" in kv)
        _open = _q.get("state", "open") == "open"
        _rest = [{"number": p["number"], "title": p.get("title"), "html_url": p.get("url"),
                  "state": "open" if _open else "closed", "body": p.get("body"),
                  "created_at": p.get("createdAt"),
                  "merged_at": None if _open else (p.get("mergedAt") or "2026-06-16T00:00:00Z")}
                 for p in _prs("open" if _open else "merged", _lm.group(1))]
        _cu = os.environ.get("FAKE_REST_CLOSED_UNMERGED")
        if _cu and not _open:
            _rest.insert(0, {"number": 950, "title": "feat: abandoned", "html_url": "u/950",
                             "state": "closed", "body": "Closes #%s" % _cu,
                             "created_at": "2026-06-16T12:00:00Z", "merged_at": None})
        print(json.dumps(_rest))
        sys.exit(0)
    sys.exit(1)
print("[]")
PYEOF
GH_FAKE_NATIVE="$(cygpath -w "$GH_FAKE" 2>/dev/null || echo "$GH_FAKE")"

# ── fake git (#1070) — honours the four read-only queries _local_overlap issues.
#   FAKE_CUR_BRANCH (default main)   -> rev-parse --abbrev-ref HEAD + a self worktree.
#   FAKE_WT_BRANCH                   -> an extra worktree on that branch (also in for-each-ref).
#   FAKE_WT_PATH                     -> that worktree's path (default /repo/wt-x). A REAL
#                                       dir here lets a case plant a .session-lock (#2623).
#   FAKE_WT_DET_SHA + FAKE_WT_DET_MSG -> a detached worktree; log -1 returns the msg.
#   FAKE_LOCAL_BRANCHES (comma-sep)  -> extra local branches (for-each-ref only).
#   FAKE_WT_STATUS (comma-sep)       -> `-C <wt> status --porcelain -z` entries (#2881).
#   FAKE_WT_STATUS_FAIL=1            -> that status call exits 1.
#   FAKE_GIT_FAIL=1                  -> exit 1 (fail-open path).
GIT_FAKE="$(mktemp).py"
cat > "$GIT_FAKE" <<'PYEOF'
import os, sys
if os.environ.get("FAKE_GIT_FAIL") == "1":
    sys.exit(1)
argv = sys.argv[1:]
if argv[:1] == ["-C"]:  # #2881: the no-lock recency probe reads a sibling's tree
    no_locks = "--no-optional-locks" in argv
    argv = [a for a in argv[2:] if a != "--no-optional-locks"]
    if argv[:1] == ["status"]:
        # Without --no-optional-locks a real status refreshes the sibling's index
        # under index.lock, so the fake refuses it: that pins the flag.
        if not no_locks or os.environ.get("FAKE_WT_STATUS_FAIL") == "1":
            sys.exit(1)
        entries = [e for e in os.environ.get("FAKE_WT_STATUS", "").split(",") if e]
        sys.stdout.write("".join(e + "\0" for e in entries)); sys.exit(0)
cur = os.environ.get("FAKE_CUR_BRANCH", "main")
if argv[:2] == ["rev-parse", "--abbrev-ref"]:
    sys.stdout.write(cur + "\n"); sys.exit(0)
if argv[:2] == ["worktree", "list"]:
    blocks = ["worktree /repo/main\nHEAD aaaaaaa\nbranch refs/heads/%s\n" % cur]
    wb = os.environ.get("FAKE_WT_BRANCH")
    if wb:
        blocks.append("worktree %s\nHEAD bbbbbbb\nbranch refs/heads/%s\n"
                      % (os.environ.get("FAKE_WT_PATH", "/repo/wt-x"), wb))
    det = os.environ.get("FAKE_WT_DET_SHA")
    if det:
        blocks.append("worktree /repo/wt-det\nHEAD %s\ndetached\n" % det)
    sys.stdout.write("\n".join(blocks) + "\n"); sys.exit(0)
if argv[:1] == ["for-each-ref"]:
    names = [cur]
    wb = os.environ.get("FAKE_WT_BRANCH")
    if wb:
        names.append(wb)
    names += [n for n in os.environ.get("FAKE_LOCAL_BRANCHES", "").split(",") if n]
    sys.stdout.write("\n".join(names) + "\n"); sys.exit(0)
if argv[:2] == ["log", "-1"]:
    if argv[-1] == os.environ.get("FAKE_WT_DET_SHA"):
        sys.stdout.write(os.environ.get("FAKE_WT_DET_MSG", "") + "\n"); sys.exit(0)
    sys.stdout.write("unrelated commit\n"); sys.exit(0)
if argv[:3] == ["remote", "get-url", "origin"]:  # #2827: REST leg's repo, no GraphQL
    url = os.environ.get("FAKE_ORIGIN_URL")
    if not url:
        sys.exit(2)
    sys.stdout.write(url + "\n"); sys.exit(0)
sys.exit(0)
PYEOF
GIT_FAKE_NATIVE="$(cygpath -w "$GIT_FAKE" 2>/dev/null || echo "$GIT_FAKE")"
trap 'rm -f "$GH_FAKE" "$GIT_FAKE"' EXIT

DUP_NATIVE="$(cygpath -w "$DUP" 2>/dev/null || echo "$DUP")"
# --no-fetch in every case: tests must not touch a real git remote. --no-local on
# the remote-behavior cases isolates them from the dev's real local branches/worktrees.
# #2831: --git is the fake too. dup_scan now resolves the cwd repo from git origin
# when `gh repo view` (GraphQL) fails, so a case that fails `repo view` to model an
# UNRESOLVABLE repo (rr/tt/yy/kk) must not silently reach this checkout's REAL
# origin. The fake answers `remote get-url origin` only when FAKE_ORIGIN_URL is set.
run() { "$PY" "$DUP_NATIVE" --prebranch --no-fetch --no-local --gh "$GH_FAKE_NATIVE" --git "$GIT_FAKE_NATIVE" "$@"; }
# Local-overlap cases (#1070): real fake-git seam, clean remote (issue OPEN), so the
# LOCAL OVERLAP advisory is the only signal. Each case passes its own --files.
run_local() { "$PY" "$DUP_NATIVE" --prebranch --no-fetch --gh "$GH_FAKE_NATIVE" --git "$GIT_FAKE_NATIVE" "$@"; }
# Deterministic recency cutoff for the merged-overlap tiering (#699): merges
# at/after this ISO instant are "recent" (HOLD), earlier ones are advisory.
# Fixture mergedAt: 901=13:30Z (recent), 902/903/904/905=earlier (old).
CUT="2026-06-16T12:00:00Z"

# (a) GO: issue OPEN, no linkage (#999), target files disjoint from all PR files
out="$(run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out"; then ok "(a) clean preflight -> GO exit 0"; else bad "(a) expected GO rc0" "rc=$rc out=[$out]"; fi

# (b) HOLD: issue CLOSED
out="$(FAKE_ISSUE_STATE=CLOSED run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -qi "CLOSED" <<< "$out"; then ok "(b) closed issue -> HOLD exit 3"; else bad "(b) expected HOLD rc3 CLOSED" "rc=$rc out=[$out]"; fi

# (c) HOLD: an open PR closing-references the target issue (linkage), files disjoint
out="$(FAKE_PR_CLOSES=999 run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "#701" <<< "$out"; then ok "(c) open PR Closes #999 -> HOLD exit 3"; else bad "(c) expected HOLD rc3 #701" "rc=$rc out=[$out]"; fi

# (d) HOLD: open PR 700's files intersect the target set, issue OPEN + no linkage
out="$(run --issue 999 --files a/foo.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "a/foo.py" <<< "$out" && grep -q "#700" <<< "$out"; then ok "(d) file overlap -> HOLD exit 3"; else bad "(d) expected HOLD rc3 a/foo.py #700" "rc=$rc out=[$out]"; fi

# (e) file-overlap normalization: ./-rooted target still matches a/foo.py
out="$(run --issue 999 --files ./a/foo.py)"; rc=$?
if [ "$rc" = "3" ] && grep -q "#700" <<< "$out"; then ok "(e) ./-rooted target normalizes -> HOLD"; else bad "(e) expected HOLD on ./a/foo.py" "rc=$rc out=[$out]"; fi

# (f) fail-open: gh entirely broken -> no signal -> GO (don't block on infra).
#     Load-bearing against #2507: a dead gh leaves the repo label empty too, so this
#     is the case that keeps exit-4 NARROW. dup_scan tells the two apart by whether
#     the issue-state query answered. Collapsing them would turn every gh outage
#     into an exit 4 -- a wider contract change than #2507 asked for.
out="$(FAKE_GH_FAIL=1 run --issue 999 --files a/foo.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out"; then ok "(f) gh failure fails open -> GO exit 0"; else bad "(f) expected GO rc0 on gh fail" "rc=$rc out=[$out]"; fi

# (g) default advisory mode unchanged: no --prebranch still exits 0 (regression guard)
out="$("$PY" "$DUP_NATIVE" --issue 999 --gh "$GH_FAKE_NATIVE")"; rc=$?
if [ "$rc" = "0" ]; then ok "(g) default advisory mode still exit 0"; else bad "(g) default mode regressed" "rc=$rc out=[$out]"; fi

# (h) HOLD: a MERGED PR closing the issue (build-window merge w/ closing kw) — linkage
out="$(FAKE_MERGED_CLOSES=999 run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "#900" <<< "$out"; then ok "(h) merged PR Closes #999 -> HOLD exit 3"; else bad "(h) expected HOLD rc3 #900" "rc=$rc out=[$out]"; fi

# (i) HOLD: a RECENTLY-MERGED PR with NO closing kw whose files overlap the target
#     set (the #678 build-window-merge gap — a merge inside the window still HOLDs)
out="$(FAKE_MERGED_OVERLAP=1 run --issue 999 --files m/recent.py --merged-since "$CUT")"; rc=$?
if [ "$rc" = "3" ] && grep -q "#901" <<< "$out" && grep -qi "MERGED" <<< "$out"; then ok "(i) recent merged-PR file overlap -> HOLD exit 3"; else bad "(i) expected HOLD rc3 #901 MERGED" "rc=$rc out=[$out]"; fi

# (j) #699 core: an OLDER merged PR overlapping a target file (issue OPEN, no open
#     overlap, no linkage) is ADVISORY only -> GO exit 0 with a 'note' line, NOT a HOLD
out="$(FAKE_MERGED_OLD=1 run --issue 999 --files m/old.py --merged-since "$CUT")"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && grep -qi "note" <<< "$out" && grep -q "#902" <<< "$out"; then ok "(j) old merged overlap -> GO exit 0 + advisory note (#699)"; else bad "(j) expected GO rc0 + note + #902" "rc=$rc out=[$out]"; fi

# (k) the #682 regression: a hot file (skills/curator/SKILL.md) with several OLD
#     merged PRs, issue OPEN, no open overlap -> GO (was a false HOLD before #699)
out="$(FAKE_MERGED_HOTFILE=1 run --issue 999 --files skills/curator/SKILL.md --merged-since "$CUT")"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && grep -q "#903" <<< "$out"; then ok "(k) hot-file old merged history -> GO exit 0 (#682 false-positive fixed)"; else bad "(k) expected GO rc0 + note #903" "rc=$rc out=[$out]"; fi

# (l) a recent AND an old merged overlap together -> HOLD (recent wins), and the
#     old one still surfaces as an advisory note (exit 3 from the recent HOLD)
out="$(FAKE_MERGED_OVERLAP=1 FAKE_MERGED_OLD=1 run --issue 999 --files m/recent.py,m/old.py --merged-since "$CUT")"; rc=$?
if [ "$rc" = "3" ] && grep -q "#901" <<< "$out" && grep -qi "note" <<< "$out" && grep -q "#902" <<< "$out"; then ok "(l) mixed recent+old merged overlap -> HOLD on recent, note on old"; else bad "(l) expected HOLD rc3 #901 + note #902" "rc=$rc out=[$out]"; fi

# (m) explicit --merged-window-min 0 (same as default): a RECENT merged overlap is
#     advisory -> GO exit 0
out="$(FAKE_MERGED_OVERLAP=1 run --issue 999 --files m/recent.py --merged-window-min 0)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && grep -qi "note" <<< "$out" && grep -q "#901" <<< "$out"; then ok "(m) --merged-window-min 0 -> all merged overlap advisory, GO"; else bad "(m) expected GO rc0 + note #901" "rc=$rc out=[$out]"; fi

# (n) default (no flags): merged overlap with old dates is advisory -> GO (clock-independent
#     since default is 0 -- all merged overlaps advisory regardless of age)
out="$(FAKE_MERGED_HOTFILE=1 run --issue 999 --files skills/curator/SKILL.md)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out"; then ok "(n) default: old merged overlap -> GO exit 0 (advisory)"; else bad "(n) expected GO rc0 default" "rc=$rc out=[$out]"; fi

# (q) #833 regression: a RECENTLY merged PR with file overlap, DEFAULT flags (no
#     --merged-since, no --merged-window-min) -> GO advisory (was a false HOLD before #833)
out="$(FAKE_MERGED_OVERLAP=1 run --issue 999 --files m/recent.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && grep -qi "note" <<< "$out" && grep -q "#901" <<< "$out"; then ok "(q) #833: recent merged overlap default flags -> GO + advisory note (not HOLD)"; else bad "(q) expected GO rc0 + note #901 (#833 regression)" "rc=$rc out=[$out]"; fi

# (o) conservative fallback: a merged overlap with NO mergedAt cannot be dated, so
#     it is treated as recent -> HOLD (never silently suppress a #678-class signal)
out="$(FAKE_MERGED_NODATE=1 run --issue 999 --files m/undated.py --merged-since "$CUT")"; rc=$?
if [ "$rc" = "3" ] && grep -q "#906" <<< "$out" && grep -qi "HOLD" <<< "$out"; then ok "(o) undated merged overlap -> conservative HOLD exit 3"; else bad "(o) expected HOLD rc3 #906" "rc=$rc out=[$out]"; fi

# (p) OPEN-PR overlap still hard-HOLDs even with --merged-window-min 0 (the tiering
#     only relaxes MERGED; a concurrent open PR on the same files is never advisory)
out="$(run --issue 999 --files a/foo.py --merged-window-min 0)"; rc=$?
if [ "$rc" = "3" ] && grep -q "#700" <<< "$out" && grep -qi "OPEN" <<< "$out"; then ok "(p) open-PR overlap HOLDs regardless of merged window"; else bad "(p) expected HOLD rc3 #700 OPEN" "rc=$rc out=[$out]"; fi

# -- skip-pr tests (#919) --------------------------------------------------------
# (r) own PR is the only linkage hit: skip it -> GO exit 0
#     FAKE_PR_CLOSES=999 adds PR 701 "Closes #999"; --skip-pr 701 suppresses it.
out="$(FAKE_PR_CLOSES=999 run --issue 999 --skip-pr 701 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out"; then ok "(r) --skip-pr suppresses own PR linkage -> GO exit 0"; else bad "(r) expected GO rc0 with own PR skipped" "rc=$rc out=[$out]"; fi

# (s) own PR skipped (linkage), but a competing open-PR file overlap still HOLDs
#     PR 700 touches a/foo.py (not skipped); PR 701 closes #999 (skipped via --skip-pr).
out="$(FAKE_PR_CLOSES=999 run --issue 999 --skip-pr 701 --files a/foo.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "#700" <<< "$out"; then ok "(s) competing PR file overlap still HOLDs when own PR is skipped"; else bad "(s) expected HOLD rc3 #700 when own linkage-PR skipped" "rc=$rc out=[$out]"; fi

# -- epic-tracker sibling-pole exemption (#996) ---------------------------------
# A long-lived epic tracker is closed by multiple sequential "Part of #N" PRs.
# Every pole after the first references the epic issue, so a linkage match on a
# *sibling* reference cried wolf on exactly the multi-PR epics it should support.
# A "Part of #N" / "Refs #N" reference is a co-PR, NOT a duplicate -> must not HOLD.

# (t) #996: a MERGED sibling pole "Part of #999" (title+body), issue OPEN, files
#     disjoint from all PRs -> GO exit 0 (sibling reference is not a duplicate),
#     AND (#1756) the sibling/non-closing-mention informational note now prints.
out="$(FAKE_MERGED_PARTOF=999 run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && ! grep -qi "HOLD" <<< "$out" \
   && grep -qi "without a closing keyword" <<< "$out" && grep -q "#910" <<< "$out"; then
  ok "(t) #996: 'Part of #N' sibling pole -> GO exit 0 + informational note (#1756)"
else
  bad "(t) expected GO rc0, no HOLD, note with #910" "rc=$rc out=[$out]"
fi

# (u) #996: a MERGED sibling via "Refs #999" in body, plain title -> GO exit 0,
#     AND (#1756) the informational note now prints.
out="$(FAKE_MERGED_REFS=999 run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && ! grep -qi "HOLD" <<< "$out" \
   && grep -qi "without a closing keyword" <<< "$out" && grep -q "#911" <<< "$out"; then
  ok "(u) #996: 'Refs #N' sibling -> GO exit 0 + informational note (#1756)"
else
  bad "(u) expected GO rc0, no HOLD, note with #911" "rc=$rc out=[$out]"
fi

# (v) #996 guard: a genuine "Closes #999" still HOLDs even alongside a "Part of #999"
#     sibling — the closing keyword wins; the sibling (#910) is surfaced only in
#     the (#1756) informational note, never as a HOLD reason.
out="$(FAKE_MERGED_CLOSES=999 FAKE_MERGED_PARTOF=999 run --issue 999 --files c/other.py)"; rc=$?
hold_block="$(echo "$out" | sed -n '/^HOLD/,$p')"
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "#900" <<< "$out" \
   && grep -q "#910" <<< "$out" && ! grep -q "#910" <<< "$hold_block"; then
  ok "(v) #996 guard: Closes #999 HOLDs (#900); sibling #910 in note only, not HOLD"
else
  bad "(v) expected HOLD rc3 #900, #910 in note (not HOLD block)" "rc=$rc out=[$out]"
fi

# -- LOCAL worktree/branch overlap advisory (#1070) ------------------------------
# The remote checks above are blind to a concurrent session's UNPUSHED worktree or
# branch already building the same issue (the build-1055 gap). _local_overlap scans
# local git and emits a loud advisory that NEVER changes the HOLD/GO exit code.

# (w) a non-current worktree on a branch slug encoding the issue -> advisory, GO exit 0
out="$(FAKE_WT_BRANCH=build-999 run_local --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && grep -q "LOCAL OVERLAP" <<< "$out" && grep -q "build-999" <<< "$out" && grep -q "branch slug" <<< "$out"; then ok "(w) worktree branch slug encodes #999 -> advisory + GO exit 0"; else bad "(w) expected GO rc0 + LOCAL OVERLAP build-999" "rc=$rc out=[$out]"; fi

# (x) a local branch (not in a worktree) whose slug encodes the issue -> advisory, GO
out="$(FAKE_LOCAL_BRANCHES=wip-999-thing run_local --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && grep -q "LOCAL OVERLAP" <<< "$out" && grep -q "wip-999-thing" <<< "$out" && grep -q "local branch" <<< "$out"; then ok "(x) local branch slug encodes #999 -> advisory + GO exit 0"; else bad "(x) expected GO rc0 + LOCAL OVERLAP wip-999-thing" "rc=$rc out=[$out]"; fi

# (y) self-match: the CURRENT branch encodes the issue -> excluded, NO advisory
out="$(FAKE_CUR_BRANCH=feat/foo-999 run_local --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && ! grep -q "LOCAL OVERLAP" <<< "$out"; then ok "(y) current branch encodes #999 -> self-excluded, no advisory"; else bad "(y) expected GO rc0 and NO LOCAL OVERLAP (self)" "rc=$rc out=[$out]"; fi

# (z) a DETACHED worktree whose HEAD commit subject names #issue -> advisory via HEAD commit
out="$(FAKE_WT_DET_SHA=deadbeef FAKE_WT_DET_MSG='fix: handle #999 edge case' run_local --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "LOCAL OVERLAP" <<< "$out" && grep -qi "detached HEAD" <<< "$out" && grep -q "HEAD commit" <<< "$out"; then ok "(z) detached worktree HEAD commit names #999 -> advisory"; else bad "(z) expected LOCAL OVERLAP via HEAD commit" "rc=$rc out=[$out]"; fi

# (aa) advisory is NEVER a HOLD: a local hit alongside a real remote HOLD (open-PR file
#      overlap) still exits 3 (the remote HOLD), and the LOCAL OVERLAP line also prints
out="$(FAKE_WT_BRANCH=build-999 run_local --issue 999 --files a/foo.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "#700" <<< "$out" && grep -q "LOCAL OVERLAP" <<< "$out" && grep -q "build-999" <<< "$out"; then ok "(aa) local advisory coexists with remote HOLD; exit stays 3 (remote-decided)"; else bad "(aa) expected HOLD rc3 #700 + LOCAL OVERLAP build-999" "rc=$rc out=[$out]"; fi

# (bb) boundary: issue 99 must NOT match branch build-999 (no false positive)
out="$(FAKE_WT_BRANCH=build-999 run_local --issue 99 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && ! grep -q "LOCAL OVERLAP" <<< "$out"; then ok "(bb) #99 does not match build-999 -> no false advisory"; else bad "(bb) expected no LOCAL OVERLAP for #99 vs build-999" "rc=$rc out=[$out]"; fi

# (cc) fail-open: a broken git yields no local signal (no crash), remote GO preserved
out="$(FAKE_GIT_FAIL=1 FAKE_WT_BRANCH=build-999 run_local --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && ! grep -q "LOCAL OVERLAP" <<< "$out"; then ok "(cc) broken git fails open -> GO exit 0, no advisory"; else bad "(cc) expected GO rc0 on git failure" "rc=$rc out=[$out]"; fi

# (dd) --no-local suppresses the advisory entirely (escape hatch)
out="$(FAKE_WT_BRANCH=build-999 run_local --issue 999 --files c/other.py --no-local)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && ! grep -q "LOCAL OVERLAP" <<< "$out"; then ok "(dd) --no-local suppresses the local advisory"; else bad "(dd) expected GO rc0 + no advisory under --no-local" "rc=$rc out=[$out]"; fi

# (ee) dedup: a branch that is BOTH a worktree branch AND a for-each-ref entry is
#      reported exactly once (the flagged-set guard)
out="$(FAKE_WT_BRANCH=build-999 run_local --issue 999 --files c/other.py)"; rc=$?
n="$(echo "$out" | grep -c "build-999")"
if [ "$rc" = "0" ] && [ "$n" = "1" ]; then ok "(ee) worktree+branch dedup -> build-999 reported exactly once"; else bad "(ee) expected exactly 1 build-999 mention" "rc=$rc count=$n out=[$out]"; fi

# ── #1609: cross-repo issue refs. A bare `--issue N` binds to the CWD repo; a
#   cross-repo target (octo/notes#31) must be repo-qualified so it can't
#   silently answer about the cwd repo's own #N. Cross-repo matching against LOCAL
#   PRs must use the qualified `owner/repo#N` form, NEVER a bare `#N` (which is a
#   DIFFERENT issue in the cwd repo). cwd repo = octo/home-repo (fake).
#   (env prefixes are inlined literally per call — `run` is a bash function, so an
#   `env VAR=..` wrapper cannot invoke it; nor can a variable-expanded prefix
#   (`$X run`) -- bash decides assignment-vs-command before expansion. So the
#   two cwd/foreign repo assignments are inlined literally on every call.)

# (ff) the incident (false HOLD): target notes#31 is OPEN, but the cwd repo's own
#      #31 is CLOSED. A bare `--issue 31` HOLDs on the wrong (closed) issue; the
#      qualified ref must query notes#31 (OPEN) and GO.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_FOREIGN_REPO=octo/notes FAKE_ISSUE_STATE=CLOSED FAKE_FOREIGN_ISSUE_STATE=OPEN \
        run --issue octo/notes#31 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out"; then ok "(ff) qualified cross-repo issue OPEN -> GO (not the cwd#31 false HOLD)"; else bad "(ff) expected GO rc0 for OPEN notes#31 while cwd#31 CLOSED" "rc=$rc out=[$out]"; fi

# (gg) the dangerous false GO: target notes#31 is CLOSED (already resolved), while
#      the cwd repo's own #31 is OPEN. A bare `--issue 31` would GO (miss it); the
#      qualified ref must query notes#31 (CLOSED) and HOLD.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_FOREIGN_REPO=octo/notes FAKE_ISSUE_STATE=OPEN FAKE_FOREIGN_ISSUE_STATE=CLOSED \
        run --issue octo/notes#31 --files c/other.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -qi "CLOSED" <<< "$out"; then ok "(gg) qualified cross-repo issue CLOSED -> HOLD (not the cwd#31 false GO)"; else bad "(gg) expected HOLD rc3 CLOSED for CLOSED notes#31 while cwd#31 OPEN" "rc=$rc out=[$out]"; fi

# (hh) cross-repo linkage: a local PR whose body carries the QUALIFIED closer
#      `Closes octo/notes#31` is a real duplicate -> HOLD.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_FOREIGN_REPO=octo/notes FAKE_PR_CLOSES_QUALIFIED=octo/notes#31 \
        run --issue octo/notes#31 --files c/other.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "#702" <<< "$out"; then ok "(hh) local PR Closes octo/notes#31 -> HOLD exit 3"; else bad "(hh) expected HOLD rc3 #702 on qualified closer" "rc=$rc out=[$out]"; fi

# (ii) the crux — bare `#31` in a cwd-repo PR is cwd#31, a DIFFERENT issue, and must
#      NOT match a notes#31 target. Bare matching REPLACED by qualified, not merely
#      supplemented: cwd PR "Closes #31" + target notes#31 -> GO (no false HOLD).
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_FOREIGN_REPO=octo/notes FAKE_PR_CLOSES=31 \
        run --issue octo/notes#31 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out"; then ok "(ii) bare cwd#31 PR does NOT match notes#31 target -> GO"; else bad "(ii) expected GO rc0 (bare cwd#31 != notes#31)" "rc=$rc out=[$out]"; fi

# (jj) same-repo qualified ref: octo/home-repo#999 == the cwd repo, so
#      bare matching still applies — a cwd PR "Closes #999" -> HOLD.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_FOREIGN_REPO=octo/notes FAKE_PR_CLOSES=999 \
        run --issue octo/home-repo#999 --files c/other.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "#701" <<< "$out"; then ok "(jj) same-repo qualified ref keeps bare matching -> HOLD"; else bad "(jj) expected HOLD rc3 #701 (qualified-but-same-repo)" "rc=$rc out=[$out]"; fi

# (kk) cwd repo undeterminable (gh repo view fails) + qualified cross-repo ref ->
#      treated as cross-repo (fail-safe): a bare cwd#31 PR must NOT match -> GO.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_FOREIGN_REPO=octo/notes FAKE_CWD_REPO_FAIL=1 FAKE_PR_CLOSES=31 \
        run --issue octo/notes#31 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out"; then ok "(kk) undeterminable cwd repo -> qualified match, no false HOLD -> GO"; else bad "(kk) expected GO rc0 (fail-safe cross-repo)" "rc=$rc out=[$out]"; fi

# (ll) malformed issue ref (not a number, not owner/repo#N) -> argparse error (exit
#      2), never a GO/HOLD verdict. Bare `#31` (no owner) is ambiguous -> rejected.
out="$("$PY" "$DUP_NATIVE" --prebranch --no-fetch --no-local --gh "$GH_FAKE_NATIVE" --issue 'not-a-ref' 2>&1)"; rc=$?
if [ "$rc" = "2" ]; then ok "(ll) malformed --issue -> argparse error exit 2"; else bad "(ll) expected exit 2 on malformed --issue" "rc=$rc out=[$out]"; fi

# (mm) bare number still works exactly as before (regression): --issue 999 -> GO
out="$(run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out"; then ok "(mm) bare --issue 999 unchanged -> GO"; else bad "(mm) bare-number path regressed" "rc=$rc out=[$out]"; fi

# ── #2502: the PR-overlap probe must be scoped to the TARGET repo, not the cwd.
#   #1609 qualified the ISSUE-state query only; `gh pr list` carried no --repo at
#   all, so `gh` resolved one from the process cwd. A scan for other-repo#762 run
#   from a home-repo worktree therefore checked home-repo's open PRs and
#   printed a bare GO -- and because nothing failed, not even the fail-open note
#   fired. Under CLAUDE.md's session-workspace rule cwd and target disagree by
#   DEFAULT, so this is the normal case, not an edge one. Measured 2026-09-03.

# (nn) the incident: the FOREIGN repo has an open PR overlapping the target files;
#      the cwd repo has none. A cwd-bound probe sees nothing -> false GO.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_FOREIGN_REPO=octo/other-repo FAKE_FOREIGN_PR_FILES=scripts/record_fill.py \
        run --issue octo/other-repo#762 --files scripts/record_fill.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "#770" <<< "$out"; then ok "(nn) foreign-repo open-PR overlap -> HOLD (not the cwd-bound false GO)"; else bad "(nn) expected HOLD rc3 #770 from the FOREIGN repo's PR list" "rc=$rc out=[$out]"; fi

# (oo) discrimination — the other direction, so the fix cannot pass by holding on
#      everything. The CWD repo has the overlapping PR (a/foo.py, #700) and the
#      foreign repo has none; the target is foreign, so the cwd PR is about a
#      DIFFERENT repository and must NOT hold.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_FOREIGN_REPO=octo/other-repo \
        run --issue octo/other-repo#762 --files a/foo.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out"; then ok "(oo) cwd-repo PR does NOT hold a foreign-repo target -> GO"; else bad "(oo) expected GO rc0 (cwd #700 overlap is a different repo)" "rc=$rc out=[$out]"; fi

# (pp) every verdict names the repo it scanned. The flag only helps someone who
#      already knows the scan is repo-bound; the person who hits this does not,
#      and the verdict line is the only signal they get.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_FOREIGN_REPO=octo/other-repo \
        run --issue octo/other-repo#762 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "octo/other-repo#762" <<< "$out"; then ok "(pp) GO verdict names the scanned repo"; else bad "(pp) expected the GO line to name octo/other-repo" "rc=$rc out=[$out]"; fi

# (qq) same, on the HOLD side, and it must name the CWD repo for a bare ref --
#      the label is the repo actually scanned, never a constant.
out="$(FAKE_CWD_REPO=octo/home-repo run --issue 999 --files a/foo.py)"; rc=$?
if [ "$rc" = "3" ] && grep -q "octo/home-repo#999" <<< "$out"; then ok "(qq) HOLD verdict names the cwd repo on a bare ref"; else bad "(qq) expected the HOLD line to name octo/home-repo" "rc=$rc out=[$out]"; fi

# (rr) #2507: with the cwd repo UNRESOLVABLE the scan is BLIND -- nothing was
#      checked -- so the verdict is UNDETERMINED (exit 4), never GO. Before this it
#      exited 0 and read as a clean pass, which is the null-reading failure exactly:
#      an empty result described the instrument, not the world. The old assertion
#      passed on WORDING alone (no repo name in the line) and never looked at the
#      exit code, so the dead exit-4 path drafted here read as covered.
out="$(FAKE_CWD_REPO_FAIL=1 run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "4" ] && grep -q "UNDETERMINED — could not resolve repo for #999" <<< "$out"; then ok "(rr) unresolvable repo -> UNDETERMINED, exit 4"; else bad "(rr) expected exit 4 + UNDETERMINED when the repo cannot be resolved" "rc=$rc out=[$out]"; fi
if ! grep -q "^GO" <<< "$out"; then ok "(rr) a blind scan never prints GO"; else bad "(rr) a blind scan must not print a GO verdict" "out=[$out]"; fi

# (ss) INSTRUMENT PROOF for (rr): the SAME inputs with a RESOLVABLE repo still exit
#      0. Without this, an exit-4 that fired on every run -- or a dup_scan that
#      crashed -- would satisfy (rr) just as well. A detector that cannot stay
#      silent under legitimate load is not a detector.
out="$(FAKE_CWD_REPO=octo/home-repo run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO — no remote overlap for octo/home-repo#999" <<< "$out"; then ok "(ss) resolvable repo on the same inputs still exits 0 (exit 4 is not universal)"; else bad "(ss) expected a normal labelled GO when the repo resolves" "rc=$rc out=[$out]"; fi

# (tt) precedence: a HOLD is a POSITIVE finding -- something was actually found --
#      so it outranks "could not determine". A blind scan that still located an
#      overlap must block with 3, not soften to 4.
out="$(FAKE_CWD_REPO_FAIL=1 run --issue 999 --files a/foo.py)"; rc=$?
if [ "$rc" = "3" ]; then ok "(tt) HOLD outranks UNDETERMINED -- a real overlap still exits 3"; else bad "(tt) expected exit 3: a positive overlap finding must not degrade to UNDETERMINED" "rc=$rc out=[$out]"; fi

# ── #2733: the verdict line is self-describing. The merged-PR advisories print
#   ABOVE the verdict, and the verdict read identically with or without them, so
#   any truncation (`| tail -6` on a 20-line preflight) turned a qualified GO into
#   a clean one -- the #2727 duplicate was filed past a gate that had said the
#   right thing. Every assertion below reads the LAST line only (the `tail -1`
#   view), because that is the view the verdict has to survive. Exit codes are
#   untouched: this is wording, and each case re-asserts its rc to prove it.

# (uu) >=1 merged-PR advisory -> the GO line itself names the count. HOTFILE = 3
#      old merged PRs on skills/curator/SKILL.md, issue OPEN, no open overlap.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_MERGED_HOTFILE=1 run --issue 999 --files skills/curator/SKILL.md)"; rc=$?
last="$(printf '%s\n' "$out" | tail -n 1)"
if [ "$rc" = "0" ] && [[ "$last" == "GO (3 merged-PR advisories above — confirm the specific defect is still unfixed) — no remote overlap for octo/home-repo#999" ]]; then ok "(uu) 3 merged-PR advisories -> the GO line names the count (tail -1 is sufficient)"; else bad "(uu) expected the LAST line to read 'GO (3 merged-PR advisories above ...) — no remote overlap ...'" "rc=$rc last=[$last] out=[$out]"; fi

# (vv) mutation control: a genuinely clean scan still prints the BARE verdict --
#      no count, no parenthetical -- so the qualifier stays a signal, not a fixture.
out="$(FAKE_CWD_REPO=octo/home-repo run --issue 999 --files c/other.py)"; rc=$?
last="$(printf '%s\n' "$out" | tail -n 1)"
if [ "$rc" = "0" ] && [[ "$last" == "GO — no remote overlap for octo/home-repo#999" ]]; then ok "(vv) clean scan keeps the bare GO line (no count when there are none)"; else bad "(vv) expected the LAST line to be exactly the bare 'GO — no remote overlap for octo/home-repo#999'" "rc=$rc last=[$last]"; fi

# (ww) singular: exactly one advisory reads '1 merged-PR advisory above'.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_MERGED_OLD=1 run --issue 999 --files m/old.py)"; rc=$?
last="$(printf '%s\n' "$out" | tail -n 1)"
if [ "$rc" = "0" ] && [[ "$last" == "GO (1 merged-PR advisory above — confirm the specific defect is still unfixed) — no remote overlap for octo/home-repo#999" ]]; then ok "(ww) a single advisory reads singular on the GO line"; else bad "(ww) expected 'GO (1 merged-PR advisory above ...)' as the LAST line" "rc=$rc last=[$last]"; fi

# (xx) HOLD carries the same qualifier: open PR 700 overlaps a/foo.py (HOLD) while
#      the 3 HOTFILE merges are advisory on SKILL.md. The HOLD opener is the line
#      the reader keys on; the reasons list follows it, so match the opener itself.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_MERGED_HOTFILE=1 run --issue 999 --files skills/curator/SKILL.md,a/foo.py)"; rc=$?
hold_line="$(printf '%s\n' "$out" | grep '^HOLD')"
if [ "$rc" = "3" ] && [[ "$hold_line" == "HOLD (3 merged-PR advisories above — confirm the specific defect is still unfixed) — do NOT branch on octo/home-repo#999:" ]]; then ok "(xx) HOLD line names the advisory count; exit stays 3"; else bad "(xx) expected 'HOLD (3 merged-PR advisories above ...) — do NOT branch on ...:' with rc 3" "rc=$rc hold_line=[$hold_line] out=[$out]"; fi
out="$(FAKE_CWD_REPO=octo/home-repo run --issue 999 --files a/foo.py)"; rc=$?
hold_line="$(printf '%s\n' "$out" | grep '^HOLD')"
if [ "$rc" = "3" ] && [[ "$hold_line" == "HOLD — do NOT branch on octo/home-repo#999:" ]]; then ok "(xx) HOLD with no advisories keeps the bare opener"; else bad "(xx) expected the bare 'HOLD — do NOT branch on octo/home-repo#999:'" "rc=$rc hold_line=[$hold_line]"; fi

# (yy) UNDETERMINED carries it too: repo unresolvable (exit 4) with 3 advisories
#      already printed. The blind verdict must still say what it depends on.
out="$(FAKE_CWD_REPO_FAIL=1 FAKE_MERGED_HOTFILE=1 run --issue 999 --files skills/curator/SKILL.md)"; rc=$?
last="$(printf '%s\n' "$out" | tail -n 1)"
if [ "$rc" = "4" ] && [[ "$last" == "UNDETERMINED (3 merged-PR advisories above — confirm the specific defect is still unfixed) — could not resolve repo for #999;"* ]]; then ok "(yy) UNDETERMINED line names the advisory count; exit stays 4"; else bad "(yy) expected 'UNDETERMINED (3 merged-PR advisories above ...) — could not resolve repo for #999; ...' with rc 4" "rc=$rc last=[$last]"; fi

# (zz) newest-first: the advisories print in descending mergedAt. The fixture
#      lists 905 (06-10), 903 (06-15), 904 (06-14) -- non-monotonic on purpose, so
#      neither the API order nor a reversed one satisfies this.
out="$(FAKE_CWD_REPO=octo/home-repo FAKE_MERGED_HOTFILE=1 run --issue 999 --files skills/curator/SKILL.md)"; rc=$?
order="$(printf '%s\n' "$out" | grep -oE 'MERGED PR #90[345]' | grep -oE '[0-9]+$' | tr '\n' ' ')"
if [ "$rc" = "0" ] && [ "$order" = "903 904 905 " ]; then ok "(zz) merged-PR advisories print newest-first (903 06-15, 904 06-14, 905 06-10)"; else bad "(zz) expected advisory order 903 904 905 (newest mergedAt first)" "rc=$rc order=[$order] out=[$out]"; fi

# ── #2623: the LOCAL OVERLAP line names the overlapping worktree's lock state.
#   The advisory reported THAT a worktree exists and nothing about whether a
#   session is live inside it, so liveness was a manual second step
#   (`session_lock.py occupied`). The line now carries `[session: …]`, read
#   through session_lock.py's own primitives (read_lock + liveness -- never a
#   hand-rolled pid probe: os.kill(pid, 0) TERMINATES the target on Windows),
#   plus the scan's own read time so a stale advisory is visibly stale rather
#   than silently authoritative. Verdict wording and exit code are UNCHANGED.
WT_DIR="$(mktemp -d)"
WT_NATIVE="$(cygpath -w "$WT_DIR" 2>/dev/null || echo "$WT_DIR")"
PIDFILE="$WT_DIR/alive.pid"
# A process provably ALIVE for the duration of these cases, reporting its NATIVE
# pid (bash's $$ / $! are msys pids on Windows -- not what OpenProcess sees).
"$PY" -c "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(90)" "$PIDFILE" &
ALIVE_BG=$!
for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do [ -s "$PIDFILE" ] && break; sleep 0.25; done
ALIVE_PID="$(cat "$PIDFILE" 2>/dev/null)"
# A pid provably DEAD: the interpreter printed it and exited.
DEAD_PID="$("$PY" -c "import os; print(os.getpid())")"
trap 'kill "$ALIVE_BG" 2>/dev/null; rm -rf "$WT_DIR"; rm -f "$GH_FAKE" "$GIT_FAKE"' EXIT
lock_json() { printf '{"schema": 2, "pid": %s, "pid_kind": "%s", "session_id": "%s", "created": "2026-09-13T00:00:00+00:00", "heartbeat": "2026-09-13T00:00:00+00:00"}' "$1" "$2" "$3"; }
run_2623() { FAKE_WT_BRANCH=build-999 FAKE_WT_PATH="$WT_NATIVE" CLAUDE_CODE_SESSION_ID=self-123 run_local --issue 999 "$@"; }

# (ab) OCCUPIED: a live session-kind lock held by ANOTHER session -> "[session: OCCUPIED",
#      "IS building"; verdict GO and exit 0 UNCHANGED (pinned so this can never become a gate)
lock_json "$ALIVE_PID" session other-session > "$WT_DIR/.session-lock"
out="$(run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && grep -q '^GO' <<< "$out" && [[ "$line" == *"[session: OCCUPIED"* ]] && [[ "$line" == *"IS building"* ]]; then ok "(ab) live lock -> [session: OCCUPIED] IS building; GO, exit stays 0"; else bad "(ab) expected GO rc0 + '[session: OCCUPIED' + 'IS building'" "rc=$rc alive_pid=[$ALIVE_PID] line=[$line]"; fi

# (ac) FREE: a session-kind lock whose holder is proven dead -> "[session: FREE", and the
#      "may be building" hedge is gone (a leftover worktree is not a concurrent build)
lock_json "$DEAD_PID" session other-session > "$WT_DIR/.session-lock"
out="$(run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: FREE"* ]] && [[ "$line" != *"may be building"* ]]; then ok "(ac) dead holder -> [session: FREE], no 'may be building' hedge"; else bad "(ac) expected '[session: FREE' without 'may be building'" "rc=$rc dead_pid=[$DEAD_PID] line=[$line]"; fi

# (ad) UNREADABLE lock -> "[session: unknown", never FREE (a null reading measures the instrument first)
printf '{not json' > "$WT_DIR/.session-lock"
out="$(run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: unknown"* ]] && [[ "$line" != *"FREE"* ]]; then ok "(ad) unreadable lock -> [session: unknown], never FREE"; else bad "(ad) expected '[session: unknown' and no FREE" "rc=$rc line=[$line]"; fi

# (ae) UNPROVABLE liveness (writer could not identify the session: pid_kind unresolved)
#      -> "[session: unknown", never FREE
lock_json null unresolved other-session > "$WT_DIR/.session-lock"
out="$(run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: unknown"* ]] && [[ "$line" != *"FREE"* ]]; then ok "(ae) unresolved-kind lock -> [session: unknown], never FREE"; else bad "(ae) expected '[session: unknown' and no FREE" "rc=$rc line=[$line]"; fi

# (af) NO lock at all -> "[session: no lock" -- an absence, not a FREE (agent-* lanes write
#      no lock), so the "may be building" hedge stays
rm -f "$WT_DIR/.session-lock"
out="$(run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: no lock"* ]] && [[ "$line" == *"may be building"* ]] && [[ "$line" != *"FREE"* ]]; then ok "(af) no lock -> [session: no lock] + 'may be building' hedge kept"; else bad "(af) expected '[session: no lock' + 'may be building', no FREE" "rc=$rc line=[$line]"; fi

# (ag) MY OWN lock (same session id) is not someone else -> "[session: FREE (own lock)"
lock_json "$ALIVE_PID" session self-123 > "$WT_DIR/.session-lock"
out="$(run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: FREE (own lock)"* ]]; then ok "(ag) own session's lock -> [session: FREE (own lock)]"; else bad "(ag) expected '[session: FREE (own lock)'" "rc=$rc line=[$line]"; fi

# (ah) the advisory carries the scan's read time (UTC, second precision) -- a
#      verdict citing external state must be visibly dated (#2623 comment B)
if [[ "$line" =~ checked\ [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z ]]; then ok "(ah) LOCAL OVERLAP line is stamped 'checked <UTC ts>'"; else bad "(ah) expected 'checked YYYY-MM-DDTHH:MM:SSZ' on the LOCAL OVERLAP line" "line=[$line]"; fi

# (ai) a HOLD citing an OPEN PR is dated too: the block ends with a 'checked' line, so a
#      stale HOLD reads as stale; the opener line itself is untouched (pinned by (xx))
out="$(FAKE_CWD_REPO=octo/home-repo run --issue 999 --files a/foo.py)"; rc=$?
stamp="$(printf '%s\n' "$out" | grep -E 'checked [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')"
if [ "$rc" = "3" ] && [ -n "$stamp" ] && [[ "$stamp" == *"re-run"* ]]; then ok "(ai) HOLD block carries a 'checked <UTC ts>' line naming the re-run remedy"; else bad "(ai) expected a 'checked <ts> … re-run' line under the HOLD block" "rc=$rc out=[$out]"; fi

# (ka) no session id: the kit reads it from CLAUDE_CODE_SESSION_ID only, so with the
#      variable unset this session's own live lock cannot be told from another's.
#      The line reads unknown and says to treat it as OCCUPIED -- never "IS building
#      it", which would name this session a concurrent builder. GO and exit 0 unchanged.
lock_json "$ALIVE_PID" session self-123 > "$WT_DIR/.session-lock"
out="$(unset CLAUDE_CODE_SESSION_ID; FAKE_WT_BRANCH=build-999 FAKE_WT_PATH="$WT_NATIVE" run_local --issue 999 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && grep -q '^GO' <<< "$out" && [[ "$line" == *"[session: unknown (no session id)]"* ]] && [[ "$line" == *"treat as OCCUPIED"* ]] && [[ "$line" != *"IS building"* ]]; then ok "(ka) no session id + live lock -> [session: unknown (no session id)], treat as OCCUPIED; GO, exit 0"; else bad "(ka) expected '[session: unknown (no session id)]' + 'treat as OCCUPIED', no 'IS building', GO rc0" "rc=$rc line=[$line]"; fi

# (kb) control: with no session id a DEAD holder still reads FREE (lock holder dead) --
#      a dead pid is not this live session, so only the live case loses its answer.
lock_json "$DEAD_PID" session other-session > "$WT_DIR/.session-lock"
out="$(unset CLAUDE_CODE_SESSION_ID; FAKE_WT_BRANCH=build-999 FAKE_WT_PATH="$WT_NATIVE" run_local --issue 999 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: FREE (lock holder dead)]"* ]]; then ok "(kb) no session id + dead lock -> [session: FREE (lock holder dead)]"; else bad "(kb) expected '[session: FREE (lock holder dead)]'" "rc=$rc line=[$line]"; fi

# ── #2881: a lockless worktree's line says when its tree was last edited.
#   `no lock` is the usual state of a project-repo worktree (13 of 16 in one project repo),
#   so a sibling mid-build and an abandoned husk printed the same line. The no-lock
#   label now carries the newest mtime among the paths `git status` lists. Verdict
#   and exit code are UNCHANGED: advisory, never a HOLD.
rm -f "$WT_DIR/.session-lock"
: > "$WT_DIR/recent.py"; : > "$WT_DIR/old.py"; : > "$WT_DIR/older.py"
touch -d "3 hours ago" "$WT_DIR/old.py"
touch -d "5 hours ago" "$WT_DIR/older.py"

# (ay) an edit inside the window -> "[session: no lock, edited <N>m ago]"; the newest
#      listed path wins over older ones; GO, exit 0, hedge kept
out="$(FAKE_WT_STATUS=" M old.py,?? recent.py, M older.py" run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && grep -q '^GO' <<< "$out" && [[ "$line" =~ \[session:\ no\ lock,\ edited\ [0-9]+m\ ago\] ]] && [[ "$line" == *"may be building"* ]]; then ok "(ay) no lock + recent edit -> [session: no lock, edited <N>m ago]; GO, exit stays 0"; else bad "(ay) expected GO rc0 + '[session: no lock, edited <N>m ago]'" "rc=$rc line=[$line]"; fi

# (az) only old edits -> "[session: no lock, idle 3h]" (the newer of the two, not the older)
out="$(FAKE_WT_STATUS=" M older.py, M old.py" run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: no lock, idle 3h]"* ]]; then ok "(az) no lock + only old edits -> [session: no lock, idle 3h]"; else bad "(az) expected '[session: no lock, idle 3h]'" "rc=$rc line=[$line]"; fi

# (ba) clean status -> "[session: no lock, idle (clean tree)]"
out="$(run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: no lock, idle (clean tree)]"* ]]; then ok "(ba) no lock + clean status -> [session: no lock, idle (clean tree)]"; else bad "(ba) expected '[session: no lock, idle (clean tree)]'" "rc=$rc line=[$line]"; fi

# (bc) the status probe fails -> "recency unknown", never idle (an unread probe is not a quiet tree)
out="$(FAKE_WT_STATUS_FAIL=1 run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: no lock, recency unknown]"* ]] && [[ "$line" != *"idle"* ]]; then ok "(bc) status probe fails -> [session: no lock, recency unknown], never idle"; else bad "(bc) expected '[session: no lock, recency unknown]' and no idle" "rc=$rc line=[$line]"; fi

# (bd) status lists only a deleted path (nothing to stat) -> "recency unknown", never clean
out="$(FAKE_WT_STATUS=" D gone.py" run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: no lock, recency unknown]"* ]]; then ok "(bd) deletion-only status -> [session: no lock, recency unknown]"; else bad "(bd) expected '[session: no lock, recency unknown]'" "rc=$rc line=[$line]"; fi

# (be) clean path: a LOCKED worktree renders exactly as before, even with a recent edit listed
lock_json "$ALIVE_PID" session other-session > "$WT_DIR/.session-lock"
out="$(FAKE_WT_STATUS="?? recent.py" run_2623 --files c/other.py)"; rc=$?
line="$(printf '%s\n' "$out" | grep 'LOCAL OVERLAP')"
if [ "$rc" = "0" ] && [[ "$line" == *"[session: OCCUPIED] references #999"* ]] && [[ "$line" != *"edited"* ]]; then ok "(be) locked worktree -> [session: OCCUPIED] unchanged, no recency suffix"; else bad "(be) expected '[session: OCCUPIED] references #999' with no recency suffix" "rc=$rc alive_pid=[$ALIVE_PID] line=[$line]"; fi

# ── #2827: a GraphQL-only limit must not read as "gh is down" ────────────────
# `gh issue view` is GraphQL. Before #2827 a GraphQL limit returned None from the
# issue-state probe, gh_alive went False, and the CLOSED-issue HOLD was skipped
# for a fail-open GO -- while REST could have answered. The probe now retries once
# over `gh api repos/<nwo>/issues/<n>`.
ERRF="$(mktemp)"
QREF="octo/home-repo#999"

# (aj) GraphQL down, REST up, issue CLOSED -> the HOLD still fires (REST's
#      lowercase `closed` normalised), and the url proves the REST leg answered.
#      PYTHONUTF8=0: on a Windows host running Python in UTF-8 mode a locale-codec
#      decode of the payload cannot fail, so the explicit utf-8 decode would go
#      unexercised; off, the locale is cp1252/cp1255 and both reject this body.
#      (Linux CI decodes UTF-8 either way -- there the case is simply green.)
out="$(PYTHONUTF8=0 FAKE_GQL_FAIL=1 FAKE_REST_STATE=closed run --issue "$QREF" --files c/other.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "3" ] && [[ "$out" == *"issue #999 is CLOSED"* ]] && [[ "$out" == *"github.com/octo/home-repo/issues/999"* ]]; then ok "(aj) GraphQL down + REST closed -> HOLD exit 3"; else bad "(aj) expected HOLD rc3 via REST" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# (ak) GraphQL down, REST up, issue open -> GO. #2827 left the PR listings
#      GraphQL-only (they said so on stderr); #2831 gave them a REST twin, so this
#      GO is a CHECKED one: no issue-state note and no PR-listing note.
out="$(FAKE_GQL_FAIL=1 run --issue "$QREF" --files c/other.py 2>"$ERRF")"; rc=$?
err="$(cat "$ERRF")"
if [ "$rc" = "0" ] && [[ "$out" == *"GO — no remote overlap for octo/home-repo#999"* ]] && [[ "$err" != *"could not verify issue"* ]] && [[ "$err" != *"could not list"* ]]; then ok "(ak) GraphQL down + REST open -> checked GO, no blindness notes"; else bad "(ak) expected a labelled GO rc0 with no issue-state/PR-list notes" "rc=$rc out=[$out] err=[$err]"; fi

# (al) BOTH transports down -> the #2507 contract is unchanged: fail-open GO exit 0
#      with the issue-state note. Clean path asserted too (rc AND verdict).
out="$(FAKE_GQL_FAIL=1 FAKE_REST_FAIL=1 run --issue "$QREF" --files c/other.py 2>"$ERRF")"; rc=$?
err="$(cat "$ERRF")"
if [ "$rc" = "0" ] && [[ "$out" == *"GO"* ]] && [[ "$out" != *"HOLD"* ]] && [[ "$err" == *"could not verify issue #999 state (fail-open)"* ]]; then ok "(al) GraphQL + REST down -> fail-open GO exit 0 (unchanged)"; else bad "(al) expected fail-open GO rc0 + note" "rc=$rc out=[$out] err=[$err]"; fi

# (am) REST answering with a PULL REQUEST is not read as the issue's state
out="$(FAKE_GQL_FAIL=1 FAKE_REST_IS_PR=1 FAKE_REST_STATE=closed run --issue "$QREF" --files c/other.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "0" ] && [[ "$out" != *"CLOSED"* ]] && [[ "$(cat "$ERRF")" == *"could not verify issue"* ]]; then ok "(am) REST PR payload refused -> no CLOSED HOLD from a PR"; else bad "(am) expected the PR payload refused" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# (an) BARE --issue: the REST repo comes from git origin (never `gh repo view`,
#      GraphQL) -> CLOSED still HOLDs, against the origin repo
out="$(FAKE_GQL_FAIL=1 FAKE_REST_STATE=closed FAKE_ORIGIN_URL=git@github.com:octo/home-repo.git run_local --issue 999 --files c/other.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "3" ] && [[ "$out" == *"github.com/octo/home-repo/issues/999"* ]]; then ok "(an) bare issue: REST repo from git origin -> HOLD exit 3"; else bad "(an) expected HOLD rc3 via origin nwo" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# (ao) BARE --issue, issue open, GraphQL down. #2827 made this UNDETERMINED: the
#      repo label and the PR listings were GraphQL-blind. #2831: the repo resolves
#      from git origin and the listings answer over REST -> a real, labelled GO.
out="$(FAKE_GQL_FAIL=1 FAKE_ORIGIN_URL=https://github.com/octo/home-repo.git run_local --issue 999 --files c/other.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "0" ] && [[ "$out" == *"GO — no remote overlap for octo/home-repo#999"* ]] && [[ "$out" != *"UNDETERMINED"* ]]; then ok "(ao) bare issue, GraphQL down -> repo from origin, checked GO exit 0"; else bad "(ao) expected a labelled GO rc0" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# ── #2831: under a GraphQL-only limit the PR legs answer over REST ───────────
# Before #2831 `gh pr list` had no REST twin: a qualified scan under the limit read
# [] from both listings and exited 0 GO -- blind in exactly the leg that HOLDs on
# a duplicate PR. `gh repo view` is GraphQL too, so the same-repo check fell back
# to cross-repo (qualified-token) matching and missed a bare `Closes #N`.
ORIGIN_SSH="git@github.com:octo/home-repo.git"

# (ap) OPEN-PR linkage over REST -> HOLD 3. Qualified SAME-repo (the form CLAUDE.md
#      prescribes): needs BOTH the /pulls twin and the git-origin cwd repo.
out="$(FAKE_GQL_FAIL=1 FAKE_PR_CLOSES=999 FAKE_ORIGIN_URL="$ORIGIN_SSH" run --issue "$QREF" --files c/other.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "3" ] && [[ "$out" == *"a PR already references/closes #999: #701"* ]]; then ok "(ap) GraphQL down: OPEN-PR linkage answers over REST -> HOLD exit 3"; else bad "(ap) expected HOLD rc3 on #701 via REST" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# (aq) OPEN-PR file overlap: PR 700's files come from REST /pulls/<n>/files -> HOLD 3
out="$(FAKE_GQL_FAIL=1 run --issue "$QREF" --files a/foo.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "3" ] && [[ "$out" == *"OPEN PR #700 changed files intersect the target set: a/foo.py"* ]]; then ok "(aq) GraphQL down: OPEN-PR file overlap answers over REST -> HOLD exit 3"; else bad "(aq) expected HOLD rc3 on PR 700 files via REST" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# (ar) MERGED linkage over REST (?state=closed, filtered to merged) -> HOLD 3 on #900
out="$(FAKE_GQL_FAIL=1 FAKE_MERGED_CLOSES=999 FAKE_ORIGIN_URL="$ORIGIN_SSH" run --issue "$QREF" --files c/other.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "3" ] && [[ "$out" == *"#900"* ]]; then ok "(ar) GraphQL down: MERGED-PR linkage answers over REST -> HOLD exit 3"; else bad "(ar) expected HOLD rc3 on #900 via REST" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# (as) instrument proof for (ar): a CLOSED-but-never-merged PR with the same
#      "Closes #999" body -- REST ?state=closed returns it, `--state merged` never
#      would -- is filtered out, not read as a merged duplicate -> GO 0
out="$(FAKE_GQL_FAIL=1 FAKE_REST_CLOSED_UNMERGED=999 FAKE_ORIGIN_URL="$ORIGIN_SSH" run --issue "$QREF" --files c/other.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "0" ] && [[ "$out" != *"#950"* ]] && [[ "$out" == *"GO — no remote overlap"* ]]; then ok "(as) closed-unmerged REST PR is not a merged duplicate -> GO exit 0"; else bad "(as) expected #950 filtered, GO rc0" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# (at) REST answers the issue but NOT /pulls: the linkage/overlap legs looked at
#      nothing -> UNDETERMINED exit 4, never a GO (a blind instrument's empty
#      result describes the instrument, not the world)
out="$(FAKE_GQL_FAIL=1 FAKE_REST_PULLS_FAIL=1 run --issue "$QREF" --files c/other.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "4" ] && [[ "$out" == *"UNDETERMINED"* ]] && [[ "$out" == *"could not list open and merged PRs"* ]] && ! grep -q "^GO" <<< "$out"; then ok "(at) PR listings blind on both transports -> UNDETERMINED exit 4"; else bad "(at) expected UNDETERMINED rc4 naming both listings" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# (au) an OPEN PR's /files unreadable: its overlap is unknown, so the open listing
#      counts as blind -> UNDETERMINED exit 4 naming the open listing only
out="$(FAKE_GQL_FAIL=1 FAKE_REST_FILES_FAIL=1 run --issue "$QREF" --files c/other.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "4" ] && [[ "$out" == *"could not list open PRs"* ]]; then ok "(au) open-PR files unreadable -> UNDETERMINED exit 4"; else bad "(au) expected UNDETERMINED rc4 naming open PRs" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# (av) precedence: a positive finding outranks the blind verdict -- /pulls down but
#      the issue is CLOSED -> HOLD 3, not 4
out="$(FAKE_GQL_FAIL=1 FAKE_REST_PULLS_FAIL=1 FAKE_REST_STATE=closed run --issue "$QREF" --files c/other.py 2>"$ERRF")"; rc=$?
if [ "$rc" = "3" ] && [[ "$out" == *"issue #999 is CLOSED"* ]]; then ok "(av) HOLD outranks blind PR listings -> exit 3"; else bad "(av) expected HOLD rc3" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi

# (aw) merged PRs listed over REST carry no files (one call per PR for a leg that is
#      advisory-only, #833): the scan SAYS the merged-file advisories went unchecked
#      rather than printing an advisory-free GO as if it had looked. Exit unchanged.
out="$(FAKE_GQL_FAIL=1 FAKE_MERGED_HOTFILE=1 run --issue "$QREF" --files skills/curator/SKILL.md 2>"$ERRF")"; rc=$?
err="$(cat "$ERRF")"
if [ "$rc" = "0" ] && [[ "$out" == *"GO"* ]] && [[ "$err" == *"merged-PR file overlap unchecked"* ]]; then ok "(aw) REST merged listing without files -> GO + 'merged-PR file overlap unchecked' note"; else bad "(aw) expected GO rc0 + unchecked note on stderr" "rc=$rc out=[$out] err=[$err]"; fi

# (ax) clean path of (aw): the same inputs with GraphQL UP print the merged advisories
#      and no 'unchecked' note -- the note is not universal
out="$(FAKE_MERGED_HOTFILE=1 run --issue "$QREF" --files skills/curator/SKILL.md 2>"$ERRF")"; rc=$?
if [ "$rc" = "0" ] && [[ "$out" == *"#903"* ]] && [[ "$(cat "$ERRF")" != *"unchecked"* ]]; then ok "(ax) GraphQL up: merged advisories present, no 'unchecked' note"; else bad "(ax) expected advisories and no note" "rc=$rc out=[$out] err=[$(cat "$ERRF")]"; fi
rm -f "$ERRF"

# -- title citation vs title closer (#3385) --------------------------------------
# (bf) the observed HOLD: PR #3370's title cited #3107 in prose and closed #3366,
#      and `--prebranch --issue 3107` exited 3. A prose citation is a note, not a HOLD.
out="$(FAKE_MERGED_TITLE_CITE=999 run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "0" ] && grep -q "GO" <<< "$out" && ! grep -qi "HOLD" <<< "$out" \
   && grep -qi "without a closing keyword" <<< "$out" && grep -q "#916" <<< "$out"; then
  ok "(bf) #3385: a prose title citation -> GO exit 0 + note naming #916"
else
  bad "(bf) expected GO rc0, no HOLD, note with #916" "rc=$rc out=[$out]"
fi

# (bg) positive control: a title-only "(Closes #N)" closer still HOLDs
out="$(FAKE_MERGED_TITLE_CLOSES=999 run --issue 999 --files c/other.py)"; rc=$?
if [ "$rc" = "3" ] && grep -qi "HOLD" <<< "$out" && grep -q "#917" <<< "$out"; then
  ok "(bg) #3385: a title-only '(Closes #N)' -> HOLD exit 3 naming #917"
else
  bad "(bg) expected HOLD rc3 #917" "rc=$rc out=[$out]"
fi

echo ""
echo "  dup-scan-prebranch: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
