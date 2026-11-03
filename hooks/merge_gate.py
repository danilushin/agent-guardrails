#!/usr/bin/env python3
"""merge_gate.py -- PreToolUse gate (matcher Bash) on merges into main and pushes to main.

An agent-issued `gh pr merge <N>` (or the REST merge, `gh api -X PUT
repos/<o>/<r>/pulls/<N>/merge`) whose base is main or master is blocked unless a
person authorized THAT PR in the last 10 minutes: a one-shot marker that
phrase_arm.py writes when the human types `mgo <N>` as their entire prompt and the
pre-merge check returns GO. The marker is deleted on the merge it allows. A merge
into any other base passes without one. An agent-issued `git push` whose destination
is main or master is blocked outright: no marker clears it, and the block hands the
human the one line to run themselves (a `!` command fires no PreToolUse hook).

What the gate reads, past the obvious forms:

  - Every branch present is evaluated (merge, REST merge, push) and any block wins;
    a marker is consumed only after every branch has passed (#3028 a).
  - Push destinations are shell-tokenised too, so "main", 'main', HEAD:"main" and
    @:main are gated, and --all, --mirror and a glob refspec count as a push to main
    (#3028 b, i).
  - Comments are stripped quote-aware, so a quoted ` #` (`-m "fix #123"`) does not
    hide the rest of its line (#3028 c). Backslash-newline continuations are folded
    first, as sh folds them, so a continuation inside a word joins the word (#3028 d,
    #3167).
  - Every `gh pr merge` segment is gated, and every REST segment that names the PUT
    (#3028 e); a quoted or attached method (-X "PUT", -XPUT) counts (#3028 f).
  - The PR number is read outside quotes first, so a number in a quoted flag value
    never stands in for it (#3028 g). The base probe asks about the repo the merge
    acts on: a PR URL's owner/repo, else --repo, else a GH_REPO= prefix, else the last
    earlier `export GH_REPO=` (#3028 h).
  - A push the scan view cannot isolate (a quoted ';', '|', '&&' or newline inside the
    git invocation) is still found in the detection view and blocks (#3028 j).
  - Quoted text, heredoc bodies fed to cat/tee/`git commit -F -` and
    `gh pr|issue create|comment|edit --body-file -` are data, never a command
    (#1421, #1471, #2815, #3161).

Fail-closed: an empty or unparseable payload blocks (Claude Code always sends one, so
an empty one means the plumbing failed). A failed or timed-out base probe blocks
unless the PR's marker is armed. Import and version (>= 3.10) errors, and any later
internal error, exit 2. hooks.json also sets `onFailure: block`, so a hook that cannot
start or times out blocks too.

Known residuals (no layer here sees them): a push whose destination is implicit
(`git push`, `git push origin HEAD` while on main), held in a variable, or wrapped in
`sh -c '...'` / eval; a `cd` earlier in the command (the merge probe runs in the
payload cwd); a command chained on the same line after a cat/tee/`git commit -F -`
heredoc operator, which the heredoc-sink regex swallows from the detection view (its
gh `--body-file -` sink allows only blanks after the marker, so it swallows none); a
` #` inside a nested (`"$(echo "a #b")"`) or ANSI-C (`$'it\\'s #1'`) quote, which the
detection view's quote-blind comment strip still reads as a comment. The marker binds
on the PR number alone, so a marker armed for #N in one repo clears a merge naming #N
in another within its TTL.

Marker directory: merge_marker.marker_dir(), i.e. $CLAUDE_PLUGIN_DATA, the same call
the writer makes. Unset, no marker is ever found (reject=nodir).

Exit 0 = allow, silent. Exit 2 = block, message on stderr (fed back to the model).
"""
import sys

# Startup: every import and the version floor sit in this try, before any code that
# needs 3.10. A failure here exits 2 (block), never 1, because a non-zero exit other
# than 2 is a non-blocking error to Claude Code -- it would fail OPEN.
try:
    import json
    import os
    import re
    import shlex
    import shutil
    import subprocess
    import time
    from datetime import datetime, timezone

    if sys.version_info < (3, 10):
        raise RuntimeError(
            "python >= 3.10 required, this is %s" % sys.version.split()[0])

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import merge_marker
except Exception as exc:  # noqa: BLE001 -- any startup failure fails CLOSED
    try:
        sys.stderr.write(
            "MERGE GATE: startup error -- failing closed (exit 2): %s\n" % (exc,))
    finally:
        sys.exit(2)

I = re.IGNORECASE   # every match below is case-insensitive unless it says otherwise

TTL_MINUTES = merge_marker.MARKER_TTL_MINUTES
GH_FALLBACKS = ("/opt/homebrew/bin/gh", "C:/Program Files/GitHub CLI/gh.exe")
# A hung gh probe counts as a failed probe, which fails closed (the marker is still
# the way through), rather than running into Claude Code's hook timeout (600 s
# default). The budget caps ALL probes of one command, since every merge segment is
# probed: ten hung probes must not add up to the hook timeout.
PROBE_TIMEOUT_S = 60
PROBE_BUDGET_S = 240

# ---- detection / extraction regexes ----
COMMENT_RE = r"(^|\s)#.*$"
HEREDOC_SINK_RE = (
    r"(?s)(?:\b(?:cat|tee)\b[^\n]*|\bgit\s+commit\b[^\n&|;]*?(?:-F|--file)[=\s]*-\s*)"
    r"""<<-?\s*(['"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2\s*(?=\n|$)"""
    r"""|\bgh\s+(?:pr|issue)\s+(?:create|comment|edit)\b[^\n&|;]*?(?:-F|--body-file)[=\s]*-\s*"""
    r"""<<-?\s*(['"]?)(\w+)\3[ \t]*\n.*?\n[ \t]*\4\s*(?=\n|$)""")
DQ_RE = r'"(?:\\.|[^"\\])*"'
SQ_RE = r"'[^']*'"
HAS_MERGE_RE = r"\bgh\s+pr\s+merge\b"
HAS_PUSH_RE = r"\bgit\b[^\n]*\bpush\b"
API_MERGE_PATH_RE = r"""repos/([^/\s"']+/[^/\s"']+)/pulls/(\d+)/merge"""
API_PUT_RE = r"""(?:-X|--method)[=\s]+["']?PUT\b"""
API_GH_RE = r"\bgh\s+api\b"
API_PULLS_RE = r"\bpulls/\d+/merge\b"
DEST_RE = r"""(?:^|\s)\+?(?:[\w./\\-]*:)?(?:refs/heads/)?(main|master)(?=$|\s|["'])"""
SEG_SPLIT_RE = r"(?:&&|\|\||;|\||\n)"
MERGE_WORD_RE = r"\bmerge\b"
PR_URL_RE = r"https?://\S+/pull/(\d+)"
PR_NUM_RE = r"(?:^|\s)(\d{1,7})(?=$|\s)"
REPO_FLAG_RE = r"(?:--repo|-R)[=\s]+(\S+)"
PUSH_SEG_RE = r"\bgit\b.*\bpush\b"
PUSH_WORD_RE = r"\bpush\b"
TARGET_RE = r"^\d{1,7}$"
ARMED_RE = r"^cc-merge-auth\.\d{1,7}\.json$"
# #3028 (b): one shlex token naming main/master as the refspec DESTINATION -- the part
# after the last ':' of `[+]<src>:<dst>`, or the whole token when there is no ':'.
DEST_TOKEN_RE = r"(?:refs/heads/)?(?:main|master)"
PUSH_ALL_FLAGS = ("--all", "--mirror")                    # #3028 (i)
CONT_RE = r"\\\r?\n"                                      # #3028 (d)
API_PUT_SCAN_RE = r"""(?<![\w-])(?:-X|--method)[=\s]*["']?PUT\b"""   # #3028 (f)
PR_URL_REPO_RE = r"https?://([^/\s]+)/([^/\s]+/[^/\s]+)/pull/\d+"    # #3028 (h)
# #3028 (h): env names are case-sensitive, so these two are matched WITHOUT re.I.
GH_REPO_PREFIX_RE = r"""^\s*(?:env\s+)?(?:\w+=\S*\s+)*GH_REPO=(["']?)([^\s"']*)\1(?=\s)"""
GH_REPO_EXPORT_RE = r"""\bexport\s+GH_REPO=(["']?)([^\s"']*)\1"""
HEREDOC_OP_RE = r"""<<(-?)[ \t]*(['"]?)(\w+)\2"""         # #3028 (c)


def _ps_str(value):
    """The string form of a JSON value: null -> '', bools as True/False."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def _strip_comments(text):
    """Per-line comment strip for the detection view. Quote-blind: that view runs it
    only after quoted bodies are neutralized, so every `#` it still sees is outside
    quotes."""
    return "\n".join(re.sub(COMMENT_RE, r"\1", ln, flags=I) for ln in text.split("\n"))


def _strip_comments_quoted(text):
    """Drop shell comments from the SCAN view, quote-aware (#3028 c).

    A comment is a `#` at the start of a word (after whitespace or at the start, as in
    COMMENT_RE) OUTSIDE quotes; it runs to the end of its line. '...' is literal,
    "..." honours backslash escapes, and quotes may span lines as they do in sh. A
    heredoc body is copied verbatim to its terminator line, so an apostrophe in a
    commit message cannot open a quote that hides every comment after it.

    A per-line strip of `(^|\\s)#.*$` with quotes included would make `-m "fix #123"
    && git push origin main` lose the push from the scan view while detection still
    saw it. Every failure mode here keeps text rather than dropping it, which fails
    closed.
    """
    out, pending = [], []
    i, n, quote = 0, len(text), None
    while i < n:
        c = text[i]
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                out.append(text[i:i + 2])
                i += 2
                continue
            if c == quote:
                quote = None
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or text[i - 1].isspace()):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif c == "<":
            if text.startswith("<<<", i):   # a here-string, not a heredoc
                out.append("<<<")
                i += 3
                continue
            m = re.compile(HEREDOC_OP_RE).match(text, i)
            if m:
                pending.append(m.group(3))
                out.append(m.group(0))
                i = m.end()
                continue
        elif c == "\n" and pending:
            out.append(c)
            i += 1
            for delim in pending:
                while i < n:
                    j = text.find("\n", i)
                    end = n if j < 0 else j + 1
                    line = text[i:end]
                    out.append(line)
                    i = end
                    if line.strip() == delim:
                        break
            pending = []
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _fold(text):
    """Join backslash-newline continuations (#3028 d). Runs AFTER the comment
    strip, because a comment line ending in a backslash does not continue in sh.
    The pair is deleted, not turned into a space (#3167): sh removes it before splitting
    words (POSIX 2.2.1), so a continuation inside a word joins the word."""
    return re.sub(CONT_RE, "", text)


def _neutralize_quotes(text):
    return re.sub(SQ_RE, "''", re.sub(DQ_RE, '""', text))


def _segments(scan):
    return re.split(SEG_SPLIT_RE, scan, flags=I)


def _segment_spans(scan):
    """(start, end) of each _segments() piece, so a segment can be placed in `scan`."""
    spans, pos = [], 0
    for m in re.finditer(SEG_SPLIT_RE, scan, I):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(scan)))
    return spans


# ---- marker protocol (validate here, delete only on an overall allow: #3028 a) ----

def marker_dir():
    """The writer's directory, $CLAUDE_PLUGIN_DATA, or None when it is unset."""
    return merge_marker.marker_dir()


def marker_path(target):
    return os.path.join(marker_dir(), merge_marker.marker_name(target))


def _age_minutes(raw_ts):
    text = _ps_str(raw_ts).strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.astimezone()   # no offset = local time
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


def check_marker(expected, session_id):
    """Validate the per-target marker WITHOUT consuming it.

    Returns (ok, path, reject). `reject` is one of these diagnostic codes:
    target | nodir | path | parse | session | age (none when ok).
    """
    if not re.search(TARGET_RE, expected, I):
        return False, "(none)", "target"
    if not marker_dir():
        # G2: no CLAUDE_PLUGIN_DATA means the writer has nowhere to put a marker
        # either, so there is none to find: fail closed and say why.
        return False, "(%s unset)" % merge_marker.DATA_DIR_ENV, "nodir"
    path = marker_path(expected)
    if not os.path.exists(path):
        # Another target armed -> reject=target, the "you armed a different PR" hint.
        mdir = marker_dir()
        try:
            names = os.listdir(mdir)
        except OSError:
            names = []
        armed = [n for n in names if re.search(ARMED_RE, n, I)
                 and os.path.isfile(os.path.join(mdir, n))]
        return False, path, ("target" if armed else "path")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            mk = json.load(fh)
    except (OSError, ValueError):
        return False, path, "parse"
    if not isinstance(mk, dict):
        return False, path, "parse"
    if _ps_str(mk.get("target")).lower() != expected.lower():
        return False, path, "target"
    # #2310: another session's marker neither clears this merge nor gets deleted by it.
    # Fails OPEN when either id is absent (pre-#2310 marker / session-less shell).
    armed_sid = _ps_str(mk.get("session_id"))
    if session_id and armed_sid and armed_sid.lower() != session_id.lower():
        return False, path, "session"
    age = _age_minutes(mk.get("ts"))
    if age is None or age < 0 or age > TTL_MINUTES:
        return False, path, "age"
    return True, path, "none"


def _diag(path, reject):
    return "(marker check: path=%s reject=%s)" % (path, reject)


def _auth_help(target):
    return "\n".join([
        "A person has to authorize this merge. Do NOT write an authorization marker",
        "yourself and do not retry the command. Surface this to the human verbatim and",
        "stop -- either of these clears it:",
        "  mgo %s" % target,
        "    typed as their ENTIRE next prompt: the plugin runs the pre-merge check and,",
        "    on GO, authorizes this one merge for %d minutes; you then run it." % TTL_MINUTES,
        "  ! <the original blocked command, re-run as-is>",
        "    run by them directly: a `!` command fires no hook.",
    ])


# ---- gh base-branch probe ----

def resolve_gh():
    found = shutil.which("gh")
    if found:
        return found
    for candidate in GH_FALLBACKS:
        if os.path.isfile(candidate):
            return candidate
    return None


def probe_base(gh, args, cwd=None, deadline=None):
    """`gh pr view ... --jq .baseRefName`, or None when the probe failed or timed out."""
    timeout = PROBE_TIMEOUT_S
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            return None   # the per-command budget is spent: a failed probe
    try:
        proc = subprocess.run([gh] + args, cwd=cwd, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):   # TimeoutExpired is a SubprocessError
        return None
    base = proc.stdout.decode("utf-8", "replace").strip()
    return base if proc.returncode == 0 and base else None


def _gh_missing(target):
    return ("MERGE GATE: gh not found on PATH or at the known install locations (%s) -- "
            "cannot verify base branch.\n%s" % (", ".join(GH_FALLBACKS), _auth_help(target)))


# Each branch returns a list of (block_message_or_None, marker_path_to_consume_or_None).

NO_PR_NUMBER = ("MERGE GATE: 'gh pr merge' without an explicit PR number -- the gate cannot "
                "verify the base branch. Re-run with the PR number (or URL), e.g. "
                "'gh pr merge 123 --squash'.")


def _merge_spans(scan):
    """Every segment carrying a `gh pr merge` (#3028 e). #2815: a segment counts when
    its OWN quote-neutralized view carries the merge; the raw first match is the
    fallback, which keeps an unbalanced split fail-closed. None stands for "no
    segment" and blocks as having no PR number."""
    spans = _segment_spans(scan)
    hits = [sp for sp in spans
            if re.search(HAS_MERGE_RE, _neutralize_quotes(scan[sp[0]:sp[1]]), I)]
    if hits:
        return hits
    return [next((sp for sp in spans if re.search(HAS_MERGE_RE, scan[sp[0]:sp[1]], I)), None)]


def _url_repo(url):
    m = re.match(PR_URL_REPO_RE, url, I)
    if not m:
        return None
    host, nwo = m.group(1), m.group(2)
    return nwo if host.lower() == "github.com" else "%s/%s" % (host, nwo)


def _pr_target(seg):
    """(pr, url_repo) of one `gh pr merge` segment, read outside quotes first (#3028 g).
    A quoted URL is still read, last; a quoted bare number is not."""
    parts = re.split(MERGE_WORD_RE, seg, maxsplit=1, flags=I)
    after = parts[1] if len(parts) > 1 else ""
    bare = _neutralize_quotes(after)
    url = re.search(PR_URL_RE, bare, I)
    if url:
        return url.group(1), _url_repo(url.group(0))
    num = re.search(PR_NUM_RE, bare, I)
    if num:
        return num.group(1), None
    url = re.search(PR_URL_RE, after, I)
    if url:
        return url.group(1), _url_repo(url.group(0))
    return None, None


def _merge_repo(scan, start, seg, url_repo):
    """The repo the merge acts on (`repo`): a PR URL's owner/repo, else --repo, else a
    GH_REPO= prefix on the segment, else the last `export GH_REPO=` before it."""
    if url_repo:
        return url_repo
    flag = re.search(REPO_FLAG_RE, seg, I)
    if flag:
        return flag.group(1)
    env = re.search(GH_REPO_PREFIX_RE, seg)
    if env:
        return env.group(2)
    exports = re.findall(GH_REPO_EXPORT_RE, scan[:start])
    return exports[-1][1] if exports else None


def _gate_merge(scan, span, cwd, session_id, deadline):
    seg = scan[span[0]:span[1]] if span else ""
    pr, url_repo = _pr_target(seg)
    if not pr:
        return NO_PR_NUMBER, None
    args = ["pr", "view", pr, "--json", "baseRefName", "--jq", ".baseRefName"]
    repo = _merge_repo(scan, span[0], seg, url_repo)
    if repo:
        args += ["--repo", repo]
    gh = resolve_gh()
    if not gh:
        return _gh_missing(pr), None
    base = probe_base(gh, args, cwd if cwd and os.path.isdir(cwd) else None, deadline)
    if base is not None and base.lower() not in ("main", "master"):
        return None, None   # e.g. feature->dev merges pass
    ok, path, reject = check_marker(pr, session_id)
    if ok:
        return None, path
    if base is None:
        return ("MERGE GATE: could not resolve the base branch of PR %s (gh probe failed) -- "
                "failing closed. Run from the repo directory or add --repo owner/name so the "
                "gate can verify the base, or authorize explicitly.\n%s\n%s"
                % (pr, _diag(path, reject), _auth_help(pr))), None
    return ("MERGE GATE: PR %s targets '%s' -- a merge into main is irreversible and needs a "
            "person's authorization for this PR. Blocked ON the merge command, "
            "pre-execution.\n%s\n%s" % (pr, base, _diag(path, reject), _auth_help(pr))), None


def merge_branch(scan, cwd, session_id, deadline=None):
    return [_gate_merge(scan, sp, cwd, session_id, deadline) for sp in _merge_spans(scan)]


def _gate_rest(seg, session_id, deadline):
    m = re.search(API_MERGE_PATH_RE, seg, I)
    if not m:
        return ("MERGE GATE: 'gh api ... /pulls/<n>/merge' without a resolvable owner/repo in "
                "the URL -- the gate cannot verify the base branch. Use the full path, e.g. "
                "'gh api -X PUT repos/owner/name/pulls/123/merge'."), None
    nwo, pr = m.group(1), m.group(2)
    gh = resolve_gh()
    if not gh:
        return _gh_missing(pr), None
    base = probe_base(gh, ["pr", "view", pr, "--repo", nwo, "--json", "baseRefName",
                           "--jq", ".baseRefName"], deadline=deadline)
    if base is not None and base.lower() not in ("main", "master"):
        return None, None   # same carve-out as `gh pr merge`
    ok, path, reject = check_marker(pr, session_id)
    if ok:
        return None, path
    if base is None:
        return ("MERGE GATE: could not resolve the base branch of %s#%s (gh probe failed) -- "
                "failing closed. Check the owner/repo in the URL, or authorize explicitly.\n"
                "%s\n%s" % (nwo, pr, _diag(path, reject), _auth_help(pr))), None
    return ("MERGE GATE: %s#%s targets '%s' -- a merge into main is irreversible and needs a "
            "person's authorization for this PR. The REST endpoint performs the SAME merge as "
            "'gh pr merge' and is gated identically. Blocked ON the command, pre-execution."
            "\n%s\n%s" % (nwo, pr, base, _diag(path, reject), _auth_help(pr))), None


def rest_branch(scan, session_id, deadline=None):
    """Every `gh api ... pulls/<n>/merge` segment that names the PUT (#3028 e); when none
    names it in-segment, every such segment; with none at all, the whole scan."""
    segs = [scan[a:b] for a, b in _segment_spans(scan)
            if re.search(API_GH_RE, scan[a:b], I) and re.search(API_PULLS_RE, scan[a:b], I)]
    put = [s for s in segs if re.search(API_PUT_SCAN_RE, s, I)]
    return [_gate_rest(s, session_id, deadline) for s in (put or segs or [scan])]


def _push_tokens(after):
    try:
        return shlex.split(after)
    except ValueError:   # unbalanced quote: drop the quotes rather than fail open
        return after.replace('"', "").replace("'", "").split()


def is_main_destination(after):
    """The destination regex, unioned with the #3028 (b) shlex token check and the
    #3028 (i) forms (--all, --mirror, a glob refspec), each of which can update main."""
    if re.search(DEST_RE, after, I):
        return True
    for tok in _push_tokens(after):
        if tok.lower() in PUSH_ALL_FLAGS:
            return True
        dst = (tok[1:] if tok.startswith("+") else tok).rsplit(":", 1)[-1]
        if re.fullmatch(DEST_TOKEN_RE, dst, I):
            return True
        if "*" in dst and (":" in tok or dst == "*" or dst.lower().startswith("refs/")):
            return True
    return False


def _push_seg_is_main(seg):
    if not re.search(PUSH_SEG_RE, seg, I):
        return False
    return is_main_destination(re.split(PUSH_WORD_RE, seg, maxsplit=1, flags=I)[1])


def _push_block(scan, start, end, cwd):
    """The block for a push-to-main found in the scan view, with ONE line for the human.

    Nothing in the blocked command ran, so the line is the command as typed up to and
    including the push -- an earlier `cd`, variable or commit comes along -- prefixed
    with `cd <payload cwd> &&`, because the human's `!` runs in their own shell's
    directory. When the text before the push spans lines (a heredoc), or carries a
    merge (`gh pr merge` or a REST merge, which the merge and REST branches gate on
    their own), the line is the push alone: a merge never rides inside the push line.
    """
    seg = scan[start:end].strip()
    head = scan[:end].strip()
    prefix = scan[:start]
    several_lines = "\n" in head
    carries_merge = bool(re.search(HAS_MERGE_RE, prefix, I) or (
        re.search(API_GH_RE, prefix, I) and re.search(API_PULLS_RE, prefix, I)))
    line = seg if several_lines or carries_merge else head
    if cwd:
        line = "cd %s && %s" % (shlex.quote(cwd.replace("\\", "/")), line)
    msg = ("MERGE GATE: 'git push' with main/master as destination is irreversible. "
           "Blocked ON the push command, pre-execution.\n"
           "Command segment: %s\n"
           "No authorization clears an agent push to main: it is the human's to run "
           "directly. Surface this one line to the human verbatim and stop -- do not "
           "retry it yourself:\n"
           "  ! %s" % (seg, line))
    if carries_merge:
        msg += ("\nNothing in the blocked command ran, and the text before the push carries "
                "a merge, which the gate checks on its own, so the line above is the push "
                "alone: a merge never rides inside the push line. Settle the merge first "
                "(its own block above, or run it yourself if it passed), then surface the "
                "line; if an earlier cd chose the repo, put that directory in the line's cd.")
    elif several_lines:
        msg += ("\nNothing in the blocked command ran, and the text before the push spans "
                "several lines, so the line above is the push alone. If the push needs an "
                "earlier line first (a commit, a variable), run that line yourself before "
                "surfacing it; if an earlier cd chose the repo, put that directory in the "
                "line's cd.")
    return msg


DETVIEW_ONLY = ("MERGE GATE: this command pushes to main/master, but the gate saw the push "
                "only with quoted text set aside (a quoted ';', '|', '&&' or newline inside "
                "the git invocation splits it), so it cannot quote the push back verbatim. "
                "Blocked ON the push command, pre-execution. Rewrite the push so no quoted "
                "value inside it holds ';', '|', '&' or a newline, and re-run it: the gate "
                "then gives you the one line to surface to the human.")


def push_branch(scan, cwd, det=""):
    # G5: no path, cwd or repo is exempt. A push to main blocks wherever it runs.
    for start, end in _segment_spans(scan):
        if _push_seg_is_main(scan[start:end]):
            return [(_push_block(scan, start, end, cwd), None)]
    # detview: the detection view keeps every unquoted destination, so a push the scan
    # view cannot isolate still blocks.
    if any(_push_seg_is_main(seg) for seg in _segments(det)):
        return [(DETVIEW_ONLY, None)]
    return [(None, None)]


# ---- payload ----

def _parse_payload(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if not text.strip():
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def views(cmd):
    """(scan, det) for one command.

    scan: comments stripped quote-aware, quotes kept -- what extraction and gating read.
    det:  the detection-only view (#1421, #1471, #2815): inert heredoc sinks and quoted
          bodies neutralized BEFORE real comments are stripped, from the ORIGINAL command.
    Both fold continuations after their comment strip.
    """
    scan = _fold(_strip_comments_quoted(cmd))
    det = re.sub(HEREDOC_SINK_RE, "cat <<STRIPPED>>", cmd)
    det = _fold(_strip_comments(_neutralize_quotes(det)))
    return scan, det


def decide(raw):
    """Return (exit_code, stderr_message). Consumes markers only on an overall allow."""
    payload = _parse_payload(raw)
    if payload is None:
        # Claude Code always sends a payload, so an empty or broken one means the
        # plumbing failed and the gate cannot see the command.
        return 2, ("MERGE GATE: empty or unparseable hook payload on stdin -- the gate "
                   "cannot see the command. Failing closed.")
    if _ps_str(payload.get("tool_name")).lower() != "bash":
        return 0, ""
    tool_input = payload.get("tool_input")
    cmd = _ps_str(tool_input.get("command")) if isinstance(tool_input, dict) else ""
    if not cmd:
        return 0, ""
    cwd = _ps_str(payload.get("cwd"))
    session_id = _ps_str(payload.get("session_id"))
    scan, det = views(cmd)

    has_merge = re.search(HAS_MERGE_RE, det, I)
    has_push = re.search(HAS_PUSH_RE, det, I)
    has_api = (re.search(API_GH_RE, det, I) and re.search(API_PULLS_RE, det, I)
               and (re.search(API_PUT_RE, det, I) or re.search(API_PUT_SCAN_RE, scan, I)))

    # #3028 (a): every branch present is evaluated; any block wins.
    deadline = time.monotonic() + PROBE_BUDGET_S
    results = []
    if has_merge:
        results += merge_branch(scan, cwd, session_id, deadline)
    if has_api:
        results += rest_branch(scan, session_id, deadline)
    if has_push:
        results += push_branch(scan, cwd, det)
    blocks = [msg for msg, _ in results if msg]
    if blocks:
        return 2, "\n\n".join(blocks)
    for path in dict.fromkeys(p for _, p in results if p):
        try:
            os.remove(path)   # one-shot: consume only now that every branch has passed
        except OSError:
            pass
    return 0, ""


def _emit(message):
    data = (message + "\n").encode("utf-8", "replace")
    buf = getattr(sys.stderr, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:
        sys.stderr.write(message + "\n")


def main():
    try:
        rc, message = decide(sys.stdin.buffer.read())
    except Exception as exc:  # noqa: BLE001 -- the gate class fails CLOSED
        rc, message = 2, "MERGE GATE: internal error -- failing closed: %r" % (exc,)
    if message:
        _emit(message)
    return rc


if __name__ == "__main__":
    sys.exit(main())
