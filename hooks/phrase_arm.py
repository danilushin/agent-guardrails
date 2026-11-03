#!/usr/bin/env python3
"""UserPromptSubmit hook: turn a human-typed `mgo <N>` into a one-shot merge authorization.

The human types one of `mgo <N>`, `merge-go <N>`, `mergeit <N>` / `mergit <N>` (any
case, each also accepted with a leading `/`) as their ENTIRE prompt. `<N>` is a bare
PR number, resolved against the session's repo, or `<owner>/<repo>#<N>` to name the
repo explicitly. This hook runs the pre-merge check (scripts/premerge_guard.py) and,
only on GO, arms the one-shot marker merge_gate.py consumes (merge_marker.arm). The
agent then merges in the same turn: one human action instead of two (type the
phrase, then paste a surfaced `!` command). On a STALE verdict it does NOT arm; it
names the mechanical clear (the guard's own `--resync`) and asks the human to re-send
the phrase.

Three invariants carry the security weight:

  FULL-MATCH ONLY -- the entire trimmed prompt must match PHRASE_RE. A phrase that
  merely APPEARS inside a longer or pasted prompt never arms. This is what kills
  the prompt-injection vector structurally rather than by filtering after the fact.

  FAIL-CLOSED TO NOT-ARMING -- a guard HOLD, a guard error, a missing guard, an
  unlaunchable subprocess, a malformed verdict and a missing CLAUDE_PLUGIN_DATA are
  all handled the same way: do not arm, and tell the agent to relay why. The worst
  case degrades to the human running the merge themselves, never to something weaker.

  HOOK-ATTESTED PROVENANCE -- the marker `note` is the verbatim prompt string read
  from this hook's own stdin payload. No agent-supplied text enters the arm path at
  any point.

Never blocks prompt submission: main() returns 0 on every path, including its own
unexpected failures.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import merge_marker  # noqa: E402

HOOKS_DIR = Path(__file__).resolve().parent
GUARD = HOOKS_DIR.parent / "scripts" / "premerge_guard.py"

# The entire trimmed prompt must match. `push` is never a target: no marker clears a
# push to main.
#
# The optional leading `/` accepts the slash-command spelling of the same words; it
# adds no target and no verb, so it cannot widen what is armable. UserPromptSubmit
# receives the raw typed text for a slash command (observed live on 2026-08-04, a
# `/merge-flow 1956` payload whose `prompt` was exactly that string). Should a future
# release deliver an expanded command body instead, the invocation is merely embedded
# in it, full-match fails, and nothing arms.
#
# Case-insensitive: a phone autocapitalizes `Mgo 2122`. Case carries no security
# weight -- FULL-MATCH ONLY is the injection kill, and an uppercase phrase typed as the
# whole prompt is the same deliberate human act. merg[ei]t is exactly one vowel --
# mergeit and mergit, never a wider typo net.
#
# #2790: the target may be qualified with the repo -- `mgo octo/other-repo#917`. A bare
# number is resolved by `gh` against the SESSION's cwd, which cannot reach a PR in a
# second repo the session also works in. The qualified form threads `--repo
# owner/repo` into the guard. Still one regex, still fullmatch: the owner is mandatory
# (`mgo other-repo#917` does not match -- an unqualified name is a guess), and the PR
# number keeps its {1,7} bound. The repo is NOT written into the marker: the marker
# binds on the number alone, so a marker armed for #917 in one repo is consumable by a
# merge line naming #917 in another within its TTL -- a recorded residual.
PHRASE_RE = re.compile(
    r"^/?(mgo|merge-?go|merge?it)\s+"
    r"(?:(?P<repo>[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100})#)?"
    r"(?P<pr>\d{1,7})$",
    re.IGNORECASE,
)

# The guard shells out to `gh`; bound the wait so a hung probe cannot wedge the
# prompt. A timeout is just another non-zero exit -> do not arm. Together with
# merge_marker.NOTIFY_TIMEOUT_S it must stay below this hook's timeout in hooks.json:
# at that limit Claude Code cancels the hook and discards its output, so a longer cap
# never fires and a slow guard dies with no reason given.
GUARD_TIMEOUT_S = 25

# Sentinel for "the guard could not be run at all". Any non-zero value works --
# callers treat every non-zero code identically for the ARM decision.
GUARD_UNAVAILABLE = 90

# premerge_guard.py's exit for a PR that is not OPEN -- ALREADY_MERGED /
# ALREADY_CLOSED (#2748). Still non-zero, so it never arms; the only thing this
# constant changes is the RELAY: "already merged, nothing to merge" in plain words
# instead of "did NOT return GO (exit 5)" + a JSON blob that reads like a HOLD the
# operator might try to clear.
GUARD_NOOP = 5


def match_target(prompt: str) -> tuple[str, str | None] | None:
    """Return (pr_number, repo_or_None) if `prompt` is EXACTLY an authorization
    phrase, else None. `repo` is `owner/repo` for the qualified form and None for
    the bare form, whose repo `gh` infers from the session cwd."""
    if not isinstance(prompt, str):
        return None
    m = PHRASE_RE.fullmatch(prompt.strip())
    if not m:
        return None
    return m.group("pr"), m.group("repo")


def match_phrase(prompt: str) -> str | None:
    """Return the PR number if `prompt` is EXACTLY an authorization phrase, else None."""
    target = match_target(prompt)
    return target[0] if target else None


def run_guard(pr: str, cwd: str | None = None, repo: str | None = None) -> tuple[int, str]:
    """Run the pre-merge guard for `pr`. Returns (exit_code, verdict_text).

    Failure to find, launch, or complete the guard is reported as a non-zero exit,
    so the caller has exactly one rule to apply: non-zero means do not arm.

    `repo` is passed through as the guard's own `--repo owner/repo`. None leaves it
    out, and `gh` infers the repo from `cwd`.
    """
    if not Path(GUARD).is_file():
        return GUARD_UNAVAILABLE, f"pre-merge guard not found at {GUARD}"
    cmd = [sys.executable, str(GUARD), "--pr", pr, "--json"]
    if repo:
        cmd += ["--repo", repo]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=GUARD_TIMEOUT_S,
            # #2294 -- the SESSION's repo: the payload cwd, else the project dir
            # Claude Code exports to hooks. A bare `mgo <N>` resolved anywhere else
            # can check an unrelated PR that happens to share the number. A cwd that
            # is not a repo makes `gh` fail, which is a non-zero guard exit ->
            # fail-closed, do not arm.
            cwd=cwd or os.environ.get("CLAUDE_PROJECT_DIR") or None,
        )
    except Exception as exc:  # noqa: BLE001 - any launch/timeout failure fails closed
        return GUARD_UNAVAILABLE, f"pre-merge guard could not be run: {exc}"
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return proc.returncode, text


# The `permission_mode` value Claude Code reports for auto mode, measured from real
# UserPromptSubmit payloads on 2026-09-05 (against `default` outside it).
AUTO_PERMISSION_MODE = "auto"


def _verdict_refs(verdict: str) -> tuple[str, str, str] | None:
    """(repo, base, head) off the guard's GO JSON, or None when the output is not
    that shape -- never a guessed ref."""
    try:
        v = json.loads(verdict)
    except (TypeError, ValueError):
        m = re.search(r"\{.*\}", verdict or "", re.S)
        try:
            v = json.loads(m.group(0)) if m else None
        except ValueError:
            v = None
    if not isinstance(v, dict):
        return None
    refs = tuple(str(v.get(k) or "") for k in ("repo", "base", "head"))
    return refs if all(refs) else None


def _reprobe_lines(pr: str, verdict: str) -> str:
    """#2822: the freshness check as a REST `compare` -- no `--no-*` flag for the
    auto-mode classifier to read as a safety bypass, and REST survives the GraphQL
    secondary limit that kills `gh pr view`."""
    refs = _verdict_refs(verdict)
    if refs:
        repo, base, headref = refs
        return (f"    gh api repos/{repo}/compare/{base}...{headref} "
                f"--jq '[.status,.behind_by]'\n")
    return (f"    gh api repos/<owner>/<name>/pulls/{pr} --jq '[.base.ref,.head.ref]'\n"
            f"    gh api repos/<owner>/<name>/compare/<base>...<head-branch> "
            f"--jq '[.status,.behind_by]'\n"
            f"    (fill <owner>/<name> from `git remote get-url origin`; the guard's "
            f"verdict did not carry the refs)\n")


def _armed_context(prompt: str, pr: str, marker: str, ts: str, mode: str = "",
                   repo: str | None = None, verdict: str = "") -> str:
    head = (
        f"agent-guardrails phrase arm: the submitted prompt is EXACTLY the authorization "
        f'phrase "{prompt}". The pre-merge guard returned GO, and this hook has '
        f"ALREADY armed the one-shot merge marker for target={pr} (10-minute TTL, "
        f"consumed on first use).\n"
        # If the merge gate ever blocks the merge below, compare this `marker=` with
        # the `path=` it echoes: a mismatch means the two hooks saw different dirs.
        f"    marker={marker} ts={ts}\n"
    )
    # #2930: `gh` resolves a bare PR number against the cwd's repo, and the marker
    # binds on the number alone, so the qualified form's three `gh pr` lines below pin
    # the repo the human named -- as the REST lines already do. The bare `mgo N` form
    # names no repo and leaves them unqualified.
    repo_flag = f" --repo {repo}" if repo else ""
    # Auto mode: most agent-issued main-targeting merges right after the phrase were
    # denied by the auto-mode classifier (measured 2026-09-05: 4 of 5 denied, 1
    # permitted). Most is not always, so auto mode keeps the in-turn instruction and
    # only ADDS the expectation that a denial is likely and is the policy working, not
    # a failure. Only an EXACT match adds the preface; an unknown value emits the
    # plain text. Mode changes the instruction, never the authorization.
    preface = ""
    if mode == AUTO_PERMISSION_MODE:
        preface = (
            f"This session reports `permission_mode: {AUTO_PERMISSION_MODE}`. Expect "
            f"the call below to be DENIED by the auto-mode classifier -- measured "
            f"2026-09-05: 4 of 5 such calls denied, 1 permitted, each the "
            f"first tool call after its authorization prompt. A denial here is "
            f"likely and is NOT a failure: it is the policy working. Attempt the "
            f"call exactly once; on a denial follow the DENIED paragraph below "
            f"(surface the `!` line, no retry, no re-arm).\n"
        )
    # #2755: the guard's GO is a snapshot with no lease -- a sibling merge 16 s
    # earlier once left a PR behind_by 1 on a base its CI never saw. The merge happens
    # a tool call later than the arm, so the agent re-probes base freshness itself
    # right before it. A moved base is STALE: no merge, the marker is left to expire,
    # and the agent runs the mechanical clear; the human re-sends the phrase once it
    # is GO. Residual: the armed marker stays live up to its TTL while the resync runs,
    # so "do NOT merge" here is an instruction, not a mechanism.
    return head + preface + (
        f"First, immediately before the merge, re-probe base freshness -- the GO "
        f"above is seconds old and has no lease (a sibling merge in that window "
        f"makes this PR's green CI a green against a moved base):\n"
        f"{_reprobe_lines(pr, verdict)}"
        f"Proceed ONLY if it prints behind_by 0 (e.g. [\"ahead\",0]). If it prints "
        f"behind_by > 0 (STALE: the base moved), do NOT merge and do NOT re-arm the "
        f"marker (it expires on its own); clear it mechanically instead -- "
        f"{_resync_instruction(prompt, pr, repo, verdict)}"
        f"On an error or no output: do NOT merge; relay what it printed to the human "
        f"verbatim and stop.\n"
        f"Run this now, in this turn:\n"
        f"    gh pr merge {pr} --squash --delete-branch{repo_flag}\n"
        # #2336: from a linked worktree that command exits 1 AFTER succeeding
        # server-side -- deleting the LOCAL branch needs the worktree moved onto the
        # base branch, and git refuses while `main` is checked out elsewhere. So a
        # non-zero rc is verified, never read as failure.
        f"If it exits NON-ZERO: do NOT conclude the merge failed and do NOT retry "
        f"it. Verify server-side state FIRST --\n"
        f"    gh pr view {pr} --json state,mergedAt{repo_flag}\n"
        f"-- and if that reports MERGED, the merge SUCCEEDED: report it as merged "
        f"and continue. A worktree-local branch-delete failure after a successful "
        f"merge is a known, cosmetic condition, not a merge failure.\n"
        # #2993: that verification read can itself be DENIED. A human-run READ
        # spends no authorization, so it is the sanctioned exit.
        f"If that verification read is itself DENIED (by the classifier or any "
        f"other gate): do NOT retry it, do NOT infer MERGED or failed from the rc, "
        f"and do NOT route around the denial with another tool -- surface "
        f"`! gh pr view {pr} --json state,mergedAt{repo_flag}` for the human to "
        f"run, and stop until it answers.\n"
        f"Do NOT re-arm the marker, do NOT surface a `! ` merge command for the human "
        f"to paste, and do NOT ask for confirmation -- the prompt you just received IS "
        f"the authorization, and it is already recorded in the marker.\n"
        f"If that exact tool call is DENIED by the auto-mode classifier or any other "
        f"PreToolUse gate: do NOT retry and do NOT re-arm the marker -- surface the "
        f"`! gh pr merge {pr} --squash --delete-branch{repo_flag}` line for the human to paste, "
        f"and note that leaving auto mode is the native remedy. The marker stays "
        f"valid for its TTL and can still be consumed that way.\n"
        # #2800: `gh pr merge` is GraphQL. An exhausted or SECONDARY limit kills it
        # outright while REST is untouched -- and `gh api rate_limit` cannot see a
        # secondary limit. The fallback is a `!` line, NEVER an agent retry: the gate
        # consumes the one-shot marker when it ALLOWS the command, before the command
        # runs, so a command that then dies on the rate limit has already spent the
        # authorization, and an agent-issued REST retry arrives with no marker.
        f"If it exits NON-ZERO, the PR still reads OPEN, and the error names a "
        f"GraphQL rate limit (`GraphQL: API rate limit already exceeded`): that is "
        f"the transport dying, not the merge being refused -- REST is unaffected, and "
        f"`gh api rate_limit` will still read 5000/5000 because a SECONDARY limit is "
        f"invisible to it. Do NOT retry `gh pr merge`, and do NOT attempt the REST "
        f"merge yourself: this marker was already consumed by the call that failed, "
        f"so an agent-issued retry is blocked with the authorization spent (and the "
        f"REST endpoint is itself gated for agent calls). Surface this line "
        f"for the human to paste, then stop:\n"
        f"    ! gh api -X PUT repos/{_rest_nwo_or_placeholder(repo)}/pulls/{pr}/merge "
        f"-f merge_method=squash\n"
        + (
            ""
            if repo else
            f"    (fill <owner>/<name> from `git remote get-url origin` in the PR's "
            f"repo -- NOT `gh repo view`, which is GraphQL and dead in this exact "
            f"scenario.)\n"
        )
        + f"A `!` command fires no PreToolUse -- the human is the authorization, the "
        f"same escape every gated command has. Afterwards verify with "
        f"`gh api repos/{_rest_nwo_or_placeholder(repo)}/pulls/{pr} --jq .merged` "
        f"(REST, unaffected); "
        f"the head branch is deleted server-side when the repo sets "
        f"delete_branch_on_merge, otherwise delete it after."
    )


def _rest_nwo_or_placeholder(repo: str | None) -> str:
    """`owner/name` for the REST URL when the phrase qualified it, else a placeholder
    the human fills. The hook deliberately does NOT shell out to resolve it: the one
    scenario this text is for is a degraded-network one."""
    return repo if repo else "<owner>/<name>"


def _not_armed_context(prompt: str, pr: str, code: int, verdict: str) -> str:
    return (
        f"agent-guardrails phrase arm: the submitted prompt is the authorization phrase "
        f'"{prompt}", but the pre-merge guard did NOT return GO for PR #{pr} '
        f"(exit {code}), so the merge marker was NOT armed. Do not merge. Relay this "
        f"verdict to the human verbatim and stop:\n{verdict or '(no guard output)'}"
    )


def _no_dir_context(prompt: str, pr: str) -> str:
    """G2: no CLAUDE_PLUGIN_DATA, so no directory the merge gate would read."""
    return (
        f"agent-guardrails phrase arm: the submitted prompt is the authorization phrase "
        f'"{prompt}", but {merge_marker.DATA_DIR_ENV} is not set in this hook\'s '
        f"environment, so there is no directory the merge gate would read a marker "
        f"from. The guard was not run and the merge marker was NOT armed for PR #{pr}. "
        f"Do not merge. Tell the human the plugin's data directory is missing (Claude "
        f"Code sets it for plugin hooks) and stop."
    )


def _noop_context(prompt: str, pr: str, verdict: str) -> str:
    """The PR is not OPEN (#2748): nothing to merge, so nothing was armed.

    Deliberately NOT _not_armed_context: that one says "did NOT return GO ... Do
    not merge", which reads as a HOLD with a condition to clear -- and the
    operator's next move on a HOLD (re-run, `--resync`, re-send the phrase) is
    wasted on a PR that is already closed.
    """
    state, when, sha = "not OPEN", "", ""
    try:
        v = json.loads(verdict)
        state = str(v.get("state") or state)
        when = str(v.get("merged_at") or v.get("closed_at") or "")
        sha = str(v.get("merge_commit") or "")[:8]
    except Exception:  # noqa: BLE001 - a malformed verdict is still relayed verbatim below
        pass
    detail = ""
    if state == "MERGED" and when:
        detail = f" (merged at {when}{', ' + sha if sha else ''})"
    elif when:
        detail = f" (at {when})"
    return (
        f"agent-guardrails phrase arm: the submitted prompt is the authorization "
        f'phrase "{prompt}", but PR #{pr} is already {state}{detail} -- there is '
        f"nothing to merge, so the merge marker was NOT armed and no authorization "
        f"was spent. Tell the human it is already {state} and stop: do not re-run "
        f"the guard, do not --resync, do not surface a merge command.\n"
        f"{verdict or '(no guard output)'}"
    )


def _verdict_json(verdict: str) -> dict:
    """The guard's --json verdict as a dict, or {} when it is not that shape."""
    try:
        v = json.loads(verdict)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _resync_line(pr: str, repo: str | None) -> str:
    """The guard's own STALE clear (`--resync`), as a runnable command.

    Full interpreter path, forward slashes. `--repo` only when known -- without it
    the guard infers the repo from the cwd, exactly as the GO-time run did."""
    py = Path(sys.executable).as_posix()
    guard = Path(GUARD).as_posix()
    repo_flag = f" --repo {repo}" if repo else ""
    return f'"{py}" "{guard}" --pr {pr}{repo_flag} --resync'


def _resync_instruction(prompt: str, pr: str, repo: str | None, verdict: str) -> str:
    """The mechanical half of a STALE hold: what to run, what it does, and that the
    merge stays behind a fresh phrase.

    `repo` is the phrase's (qualified form) or else the guard verdict's.
    """
    repo = repo or str(_verdict_json(verdict).get("repo") or "") or None
    return (
        f"run this in the background (it is the guard's own STALE clear: a "
        f"server-side update-branch -- base merged INTO the PR branch, never a merge "
        f"to main -- then a wait for CI on the NEW head, then a re-judge):\n"
        f"    {_resync_line(pr, repo)}\n"
        f"When it reports GO, tell the human to re-send `{prompt}` -- that re-send is "
        f"the authorization; do NOT merge on the resync's GO yourself. If it HOLDs "
        f"(a conflict, red CI, a timeout), relay its verdict verbatim and stop.\n"
    )


def _stale_context(prompt: str, pr: str, repo: str | None, code: int, verdict: str) -> str:
    """A STALE guard verdict: not armed, and the clear is mechanical.

    Still says "did NOT return GO" -- true, and pinned -- but unlike
    _not_armed_context it hands over the one command that clears it.
    """
    return (
        f"agent-guardrails phrase arm: the submitted prompt is the authorization "
        f'phrase "{prompt}", but the pre-merge guard did NOT return GO for PR #{pr} '
        f"(exit {code}): STALE -- its green CI ran against a base that has since "
        f"moved -- so the merge marker was NOT armed. Do not merge. The clear is "
        f"mechanical, so do it now: "
        f"{_resync_instruction(prompt, pr, repo, verdict)}"
        f"Guard verdict:\n{verdict or '(no guard output)'}"
    )


def run(payload: dict) -> str:
    """Core logic, decoupled from stdin/stdout for testability.

    Returns the additionalContext string, or "" when the hook has nothing to say
    (which is every prompt that is not an exact authorization phrase).
    """
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return ""
    prompt = prompt.strip()

    target = match_target(prompt)
    if target is None:
        return ""
    pr, repo = target

    if not merge_marker.marker_dir():
        return _no_dir_context(prompt, pr)

    code, verdict = run_guard(pr, cwd=payload.get("cwd"), repo=repo)
    if code == GUARD_NOOP:
        return _noop_context(prompt, pr, verdict)
    if code != 0:
        # Only a STALE verdict gets the mechanical clear; every other hold
        # (DUPLICATE, NOT_GREEN, an error) needs judgment and keeps the plain relay.
        if _verdict_json(verdict).get("verdict") == "STALE":
            return _stale_context(prompt, pr, repo, code, verdict)
        return _not_armed_context(prompt, pr, code, verdict)

    # One marker file per target, so a sibling session's pending arm for a different
    # PR can neither refuse nor be clobbered by this one.
    armed = merge_marker.arm(target=pr, note=prompt,
                             session_id=str(payload.get("session_id") or ""))
    return _armed_context(prompt, pr, merge_marker.marker_path(pr), armed["ts"],
                          str(payload.get("permission_mode") or ""), repo, verdict)


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:  # noqa: BLE001 - a hook must never block prompt submission
        return 0

    try:
        context = run(payload)
    except Exception:  # noqa: BLE001 - ditto; degrades to the human running the merge
        return 0

    if context:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
