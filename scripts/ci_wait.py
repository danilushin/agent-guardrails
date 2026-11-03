#!/usr/bin/env python3
"""ci_wait.py -- the one CI waiter every session reaches for (#2549, direction b).

    python scripts/ci_wait.py --pr 2575 --repo owner/name
    python scripts/ci_wait.py --sha 2845ded7 --repo owner/name --deadline 900

Prints ONE ASCII line and exits 0 GREEN / 1 RED / 2 UNDETERMINED / 3 TIMEOUT.

WHY THIS EXISTS
---------------
Four sessions in two days (2026-09-04/05) each hand-rolled a CI wait, and three of
them exhausted the account's shared 5000/hr GitHub GraphQL quota doing it: three
parallel lanes on `gh pr checks --watch`, then a solo session on `gh pr view --json
statusCheckRollup` every 20s. The cost was not abstract -- a merge-authorization
guard downstream ran blind on the empty quota, refused a green PR as UNDETERMINED,
and the merge had to be fired by hand. #2549 recorded the smell; its direction (a)
("lanes stop at push+PR, the orchestrator owns the watch") cannot cover the fourth
instance, which had no lanes and no orchestrator. A tool can.

`premerge_guard.py --wait-green` already waits for CI, and is the right thing at
MERGE time -- but it polls `gh pr view` (GraphQL) at 20s, and is coupled to the
STALE and duplicate holds with no way to opt out of STALE. A lane that just pushed
and wants to know "did my checks pass?" does not want a hold for being one commit
behind main. So this is a thin sibling, not a replacement: it reuses that module's
`_gh_json` / `_check_outcome` / `_match_check` and adds nothing to the merge gate.

WHAT IT PINS (each one a burn)
------------------------------
* REST ONLY. `repos/{repo}/commits/{sha}/check-runs`, for --pr
  `repos/{repo}/pulls/{n}`, and for a run concluding `failure`
  `repos/{repo}/check-runs/{id}/annotations` (once per run id). Never
  `gh pr view` / `gh pr checks`: those are GraphQL, and GraphQL is the bucket
  that ran dry.
* RE-RESOLVE THE HEAD. `--pr` re-reads `head.sha` on every poll. An
  `update-branch` mid-wait moves the head (2026-09-05, PR #2575), and a watcher
  pinned to the old SHA waits on runs that will never conclude.
* NEWEST RUN PER NAME WINS. check-runs keep superseded runs on the same SHA. On
  #2575 the rollup carried `labels: CANCELLED` (superseded) beside `labels:
  SUCCESS`; read naively that is red. Runs are grouped by name and the highest id
  is the verdict.
* FOUR VERDICTS, NEVER CONFLATED. GREEN = every latest run success (skipped /
  neutral siblings tolerated, but skipped-only is NOT green). RED = a confirmed
  failure / timed_out / action_required / startup_failure -- returned at once.
  UNDETERMINED = the INSTRUMENT could not answer at the deadline (API errors, or
  a `cancelled` run nothing ever replaced -- a cancel is "did not finish", not
  "failed"). TIMEOUT = the instrument answered fine and CI was still pending,
  absent, or skipped-only. An API failure is never reported as TIMEOUT, a slow
  CI never as UNDETERMINED, and neither ever as GREEN.
* A DEAD RUNNER IS NOT A RED (#2937). When the runner goes away mid-step (its
  service stopped, its host restarted), GitHub cancels the step but concludes
  the CHECK RUN `failure`. On 2026-10-01 an unattended-upgrade restarted
  cx33-runner and this tool printed RED on main with every test that ran
  passing. Such a run's only failure-level annotation is GitHub's cancel
  message, so it scores as a cancel; any other failure annotation (a genuine
  `Process completed with exit code N.`, a timeout), none at all, a full page,
  or an unreadable one keeps it RED, as before.
* A NULL IS A NULL. Zero check-runs for the whole deadline prints
  `checks_seen=0 -- no check-runs ever appeared`. Nothing reported is not
  nothing failed.
* SOME IS NOT ALL (#2987). On PR #2985 the first poll printed `CI GREEN ...
  checks=labels:success elapsed=2s`: the label guard finishes in seconds, and
  the `agent-os tests` run on that head was created ~110 s later. "Every
  check-run" had meant every check-run present at poll time. So by default (the
  CLI, without --required) GREEN also needs a check-run from the repo's required
  workflow, resolved over REST like `premerge_guard.resolve_required_check`:
  `agent-os tests` when the repo has it, else the repo's only workflow. Until one
  appears the state is `awaiting_workflow`, which at the deadline is TIMEOUT.
  REST check-runs carry job names only, so the workflow comes from
  `actions/runs?head_sha=<40 chars>` joined on the run id in `details_url`. An
  8-char head_sha there returns zero rows with no error, so the join takes the
  check-runs' own full `head_sha`, and a join or probe that fails is an
  instrument failure (UNDETERMINED), never an absent workflow (TIMEOUT). A repo
  with several workflows and none named `agent-os tests` has nothing to resolve:
  stderr says so and GREEN keeps its old meaning there.
* INTERVAL FLOOR 30s, DEFAULT 30s. Anything lower is raised and said so. On a
  rate-limit error the interval doubles (cap 300s) instead of hammering; a
  generic error keeps the interval and simply retries until the deadline. The
  default was 60s until #3303: once CI's median fell to ~8 min, a 60s poll
  noticed the finished run ~30s late on average. A pending `--pr` poll is 2 REST
  calls (pull head + check-runs), so an 8-min wait at 30s costs ~32 calls.
* NO BUDGET FROM `gh api rate_limit`. During the 2026-09-05 outage that endpoint
  reported `graphql: {remaining: 5000, used: 0}` while every GraphQL call failed.
  It is exempt from the limit and describes a different bucket. This tool never
  consults it; a real call failing is the only signal it trusts.
* NO SILENT CWD REPO. Without --repo the cwd origin is used AND NAMED on stderr;
  if it cannot be read the answer is UNDETERMINED, not a guess (#2502 / #2506
  were both "the tool quietly used the wrong repo").

[spec:none adr:none issue:#2549]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import premerge_guard as pg  # noqa: E402  -- _gh_json / _check_outcome / _match_check / _git / _nwo_from_url

GREEN, RED, UNDETERMINED, TIMEOUT = 0, 1, 2, 3

INTERVAL_DEFAULT = 30.0
INTERVAL_FLOOR = 30.0
DEADLINE_DEFAULT = 1800.0
BACKOFF_MAX = 300.0

# Conclusions that mean "did not finish" rather than "failed". `stale` is what
# GitHub stamps on a run when the head it ran against was superseded.
_INCOMPLETE = ("cancelled", "stale")

# The failure annotation GitHub writes when it abandons a job because the runner
# went away mid-step. Verbatim from job 110242831485 (2026-10-01, #2937), whose
# check run concluded `failure` with this as its ONLY annotation.
RUNNER_ABANDONED_MESSAGE = "The operation was canceled."
# One page of annotations. A run with this many cannot be a bare abandonment, and a
# later page could hold a failure annotation this page does not show.
ANNOTATIONS_PAGE = 100

# wait(workflow=AUTO): resolve the repo's required workflow over REST on first need.
AUTO = object()

EXIT_CODES_TEXT = """\
exit codes:
  0  GREEN         every latest check-run on the head succeeded (skipped/neutral tolerated),
                   and without --required one of them is from the repo's required workflow
  1  RED           a check-run concluded failure / timed_out / action_required / startup_failure
  2  UNDETERMINED  the instrument could not answer: API errors at the deadline, an unresolvable
                   repo, or a cancelled run that nothing replaced -- including a `failure` the
                   runner abandoned mid-job (#2937) -- NOT a statement about CI
  3  TIMEOUT       GitHub answered fine and CI was still pending / absent / skipped-only, or the
                   required workflow had no check-run yet, at the deadline

Never polls GraphQL (`gh pr view` / `gh pr checks`); never consults `gh api rate_limit`.
"""


def _ascii(text) -> str:
    return str(text if text is not None else "").encode("ascii", "backslashreplace").decode("ascii")


def _stderr(msg: str) -> None:
    print(_ascii(msg), file=sys.stderr)


# ------------------------------------------------------------------ pure core

def latest_per_name(check_runs):
    """One entry per check name: the run with the highest id.

    `repos/.../check-runs` returns every run ever attached to the SHA, superseded
    ones included. Ids are monotonic, so the highest is the newest. List order is
    NOT a proxy for recency (pinned by test)."""
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


def runner_abandoned(annotations) -> bool:
    """Is this `failure` a job the runner abandoned, rather than one a step failed?

    True only when every failure-level annotation is RUNNER_ABANDONED_MESSAGE.
    Warnings and notices do not count. A genuine failure carries `Process completed
    with exit code N.`, and a timeout or a lost runner adds its own failure
    annotation beside the cancel; both stay RED. No failure-level annotation at all
    is no evidence of a cancel, and a full page may hide one: both stay RED too."""
    if not isinstance(annotations, list) or len(annotations) >= ANNOTATIONS_PAGE:
        return False
    failures = [a for a in annotations if isinstance(a, dict)
                and str(a.get("annotation_level") or "").lower() == "failure"]
    return bool(failures) and all(
        str(a.get("message") or "").strip() == RUNNER_ABANDONED_MESSAGE for a in failures)


def _outcome(run, abandoned=()) -> str:
    """'success' | 'pending' | 'failure' | 'skipped' | 'cancelled'.

    premerge_guard._check_outcome folds CANCELLED into 'failure' because at MERGE
    time anything not green is a hold. Here a cancel is its own state: it carries
    no verdict about the code, so it must not read as RED (nor as GREEN). A
    `failure` whose id is in `abandoned` is a cancel too: the runner went away
    mid-job (#2937, see runner_abandoned)."""
    status = str(run.get("status") or "").lower()
    if status and status != "completed":
        return "pending"
    conclusion = str(run.get("conclusion") or "").lower()
    if conclusion in _INCOMPLETE:
        return "cancelled"
    if conclusion == "failure" and run.get("id") in abandoned:
        return "cancelled"
    return pg._check_outcome(run)


def classify(check_runs, required=None, abandoned=(), workflow=None, workflow_names=None):
    """Pure verdict over one check-runs payload.

    `abandoned` holds the ids of `failure` runs whose runner went away mid-job
    (see runner_abandoned); they score as cancels, not as RED.

    `workflow` (#2987): a green set holds no run from this workflow -- looked up
    in `workflow_names` (Actions run id -> workflow name) -- reads
    'awaiting_workflow', not 'green'.

    state: 'green' | 'red' | 'pending' | 'cancelled' | 'skipped_only' | 'empty'
           | 'awaiting_workflow'
    checks: [{name, status, conclusion, outcome, url}] -- newest per name, filtered
    failing: [{name, conclusion, url}] -- the RED evidence, actionable in one step
    """
    runs = latest_per_name(check_runs)
    if required:
        runs = [r for r in runs if pg._match_check(r, required)]
    checks = []
    for r in runs:
        checks.append({
            "name": str(r.get("name") or "?"),
            "status": str(r.get("status") or "").lower() or None,
            "conclusion": str(r.get("conclusion") or "").lower() or None,
            "outcome": _outcome(r, abandoned),
            "url": str(r.get("html_url") or r.get("details_url") or ""),
        })
    outcomes = [c["outcome"] for c in checks]
    if not checks:
        state = "empty"
    elif "failure" in outcomes:
        state = "red"
    elif "pending" in outcomes:
        state = "pending"
    elif "cancelled" in outcomes:
        state = "cancelled"
    elif "success" in outcomes:
        state = "green"
    else:
        state = "skipped_only"
    if state == "green" and workflow:
        names = workflow_names or {}
        if not any(pg._match_check({"workflowName": names.get(pg._run_id(r) or "")}, workflow)
                   for r in runs):
            state = "awaiting_workflow"
    failing =[{"name": c["name"], "conclusion": c["conclusion"], "url": c["url"]}
               for c in checks if c["outcome"] == "failure"]
    return {"state": state, "checks": checks, "failing": failing}


# ------------------------------------------------------------------ the wait

def _abandoned_ids(check_runs, required, *, repo, gh, gh_json, cache, log):
    """Ids of the latest `failure` runs whose runner abandoned them (#2937).

    One REST annotations read per run id: a completed run's annotations are final,
    so `cache` (run id -> bool) carries the answer across polls. A read that fails
    is no evidence of a cancel; the run stays a failure, i.e. RED, which is what
    this tool reported before the check existed."""
    runs = latest_per_name(check_runs)
    if required:
        runs = [r for r in runs if pg._match_check(r, required)]
    ids = set()
    for r in runs:
        status = str(r.get("status") or "").lower()
        conclusion = str(r.get("conclusion") or "").lower()
        rid = r.get("id")
        if (status and status != "completed") or conclusion != "failure" or rid is None:
            continue
        name = str(r.get("name") or "?")
        if rid not in cache:
            try:
                annotations = gh_json(gh, ["api", "repos/%s/check-runs/%s/annotations?per_page=%d"
                                           % (repo, rid, ANNOTATIONS_PAGE)])
            except Exception as exc:  # noqa: BLE001 -- unreadable is no evidence; it stays RED
                log("ci_wait: annotations of check %r (id %s) unreadable (%s); it stays a failure"
                    % (name, rid, _ascii(exc)[:140]))
                continue
            cache[rid] = runner_abandoned(annotations)
            if cache[rid]:
                log("ci_wait: check %r (id %s) concluded failure, but its only failure annotation "
                    "is %r -- the runner abandoned it mid-job (#2937): no verdict about the code; "
                    "waiting for a re-run until the deadline" % (name, rid, RUNNER_ABANDONED_MESSAGE))
        if cache[rid]:
            ids.add(rid)
    return ids


def _resolve_workflow(*, repo, gh, gh_json, log):
    """The workflow GREEN must include on `repo`, or None when there is none to pick.

    Same precedence as `premerge_guard.resolve_required_check` after its explicit
    flag: the plugin's `required_check` option when set (no probe), else the repo's
    only workflow. Where that is ambiguous the merge gate falls back to its strict
    sentinel; a waiter doing that would TIMEOUT on every such repo, so here it is
    None, said on stderr. A probe that fails raises: the caller counts it as an
    instrument failure and retries."""
    configured = (os.environ.get(pg.REQUIRED_CHECK_ENV) or "").strip()
    if configured:
        return configured
    payload = gh_json(gh, ["api", "repos/%s/actions/workflows" % repo]) or {}
    names = [str(w.get("name")) for w in payload.get("workflows") or []
             if isinstance(w, dict) and w.get("name")]
    if len(names) == 1:
        return names[0]
    log("ci_wait: %s has %d workflows and the plugin's required_check option is unset -- no "
        "required workflow to wait for, so GREEN covers only the check-runs present when it "
        "is read; pass --required <check> to pin one (#2987)" % (repo, len(names)))
    return None


def _workflow_names(check_runs, head, *, repo, gh, gh_json):
    """{Actions run id (str): workflow name} for the head, over REST.

    `actions/runs?head_sha=` needs the full 40-char SHA (an 8-char one answers zero
    rows with no error), so a short --sha borrows the check-runs' own `head_sha`.
    Raises when there is no full SHA or the call fails: an unreadable join is an
    instrument failure, never "the workflow is absent"."""
    full = str(head or "")
    if len(full) != 40:
        full = next((str(r.get("head_sha")) for r in check_runs or []
                     if isinstance(r, dict) and len(str(r.get("head_sha") or "")) == 40), "")
    if len(full) != 40:
        raise RuntimeError("no full 40-char head sha for the workflow-name join (got %r)" % head)
    payload = gh_json(gh, ["api", "repos/%s/actions/runs?head_sha=%s&per_page=100"
                           % (repo, full)]) or {}
    return {str(r.get("id")): str(r.get("name")) for r in payload.get("workflow_runs") or []
            if isinstance(r, dict) and r.get("id") is not None and r.get("name")}


def _verdict(name, code, *, repo, pr, sha, required, c, polls, failures, error, elapsed,
             workflow=None):
    checks = (c or {}).get("checks") or []
    return {
        "verdict": name, "exit_code": code, "repo": repo, "pr": pr, "sha": sha,
        "required": required, "workflow": workflow, "state": (c or {}).get("state"),
        "checks": checks, "failing": (c or {}).get("failing") or [],
        "checks_seen": len(checks), "polls": polls, "instrument_failures": failures,
        "error": error, "elapsed_s": int(round(elapsed)),
    }


def wait(*, repo, pr=None, sha=None, required=None, workflow=None, interval=INTERVAL_DEFAULT,
         deadline=DEADLINE_DEFAULT, gh="gh", gh_json=None,
         sleep=time.sleep, now=time.monotonic, log=None):
    """Poll until a verdict or the deadline. Returns the verdict dict (see _verdict).

    Exactly one of pr= / sha=. `workflow` (#2987): a workflow name GREEN must
    include a check-run from, AUTO to resolve the repo's own on first need, or None
    for no such requirement. The CLI passes AUTO unless --required is given.
    `gh_json`, `sleep`, `now`, `log` are injectable so tests never touch the network
    or the clock."""
    if (pr is None) == (sha is None):
        raise ValueError("pass exactly one of pr= / sha=")
    gh_json = gh_json or pg._gh_json
    log = log or _stderr
    interval = float(interval)
    if interval < INTERVAL_FLOOR:
        log("ci_wait: interval %gs is below the %gs floor -- raised to the floor "
            "(the floor is the point: #2549)" % (interval, INTERVAL_FLOOR))
        interval = INTERVAL_FLOOR

    start = now()
    end = start + max(0.0, float(deadline))
    polls = failures = 0
    last_error = None
    cur = interval
    head = sha
    c = None
    abandoned_cache = {}

    while True:
        polls += 1
        bump = False
        try:
            if pr is not None:
                pinfo = gh_json(gh, ["api", "repos/%s/pulls/%s" % (repo, pr)]) or {}
                new_head = str((pinfo.get("head") or {}).get("sha") or "")
                if not new_head:
                    raise RuntimeError("pull payload carried no head.sha")
                if head and new_head != head:
                    log("ci_wait: head moved %s -> %s (update-branch or new push); "
                        "following it" % (head[:8], new_head[:8]))
                head = new_head
            payload = gh_json(gh, ["api", "repos/%s/commits/%s/check-runs" % (repo, head)]) or {}
            check_runs = payload.get("check_runs") or []
            abandoned = _abandoned_ids(check_runs, required, repo=repo, gh=gh, gh_json=gh_json,
                                       cache=abandoned_cache, log=log)
            c = classify(check_runs, required, abandoned)
            if c["state"] == "green" and workflow is not None:
                if workflow is AUTO:
                    workflow = _resolve_workflow(repo=repo, gh=gh, gh_json=gh_json, log=log)
                if workflow:
                    names = _workflow_names(check_runs, head, repo=repo, gh=gh, gh_json=gh_json)
                    c = classify(check_runs, required, abandoned, workflow, names)
            last_error = None
            cur = interval
        except Exception as exc:  # noqa: BLE001 -- the instrument failed; that is data, not a crash
            failures += 1
            last_error = str(exc)
            c = None
            bump = "rate limit" in last_error.lower()
            log("ci_wait: poll %d failed (%s); retrying in %gs" % (polls, _ascii(last_error)[:140], cur))

        common = dict(repo=repo, pr=pr, sha=head, required=required, c=c, polls=polls,
                      failures=failures, error=last_error, elapsed=now() - start,
                      workflow=None if workflow is AUTO else workflow)
        if c is not None and c["state"] == "green":
            return _verdict("GREEN", GREEN, **common)
        if c is not None and c["state"] == "red":
            return _verdict("RED", RED, **common)

        t = now()
        if t >= end:
            if c is None:
                return _verdict("UNDETERMINED", UNDETERMINED, **common)
            if c["state"] == "cancelled":
                return _verdict("UNDETERMINED", UNDETERMINED, **common)
            return _verdict("TIMEOUT", TIMEOUT, **common)

        sleep(min(cur, max(0.1, end - t)))
        if bump:
            cur = min(BACKOFF_MAX, cur * 2)


# ------------------------------------------------------------------ rendering

def format_line(v) -> str:
    """One ASCII line, verdict first, machine-greppable, human-readable."""
    parts = ["CI %s" % v["verdict"]]
    if v.get("pr") is not None:
        parts.append("pr=%s" % v["pr"])
    if v.get("sha"):
        parts.append("sha=%s" % str(v["sha"])[:8])
    if v.get("required"):
        parts.append("required=%s" % v["required"])
    state = v.get("state")
    checks = v.get("checks") or []
    verdict = v["verdict"]
    if verdict == "GREEN":
        parts.append("checks=" + ",".join("%s:%s" % (c["name"], c["conclusion"] or c["outcome"])
                                          for c in checks))
    elif verdict == "RED":
        parts.append("failing=" + ",".join("%s:%s %s" % (f["name"], f["conclusion"], f["url"])
                                           for f in v.get("failing") or []))
    elif verdict == "UNDETERMINED":
        if v.get("error"):
            parts.append("error=%s" % v["error"])
        if state == "cancelled":
            parts.append("cancelled=" + ",".join(c["name"] for c in checks
                                                 if c["outcome"] == "cancelled")
                         + " -- a cancelled run nothing replaced: no verdict about the code")
    elif verdict == "TIMEOUT":
        parts.append("state=%s" % state)
        seen = v.get("checks_seen", len(checks))
        parts.append("checks_seen=%d" % seen)
        if state == "awaiting_workflow":
            parts.append("-- no check-run from the required workflow '%s' on this sha yet: "
                         "the runs that are here are not CI (#2987)" % v.get("workflow"))
        elif seen == 0:
            if v.get("required"):
                parts.append("-- no check-runs matching the required name ever appeared on this sha")
            else:
                parts.append("-- no check-runs ever appeared on this sha: nothing reported, not nothing failed")
    parts.append("elapsed=%ss" % v.get("elapsed_s", 0))
    parts.append("polls=%s" % v.get("polls", 0))
    if v.get("instrument_failures"):
        parts.append("instrument_failures=%s" % v["instrument_failures"])
    return _ascii(" ".join(parts))


# ------------------------------------------------------------------ CLI

def infer_repo(cwd=None, git=None):
    """`owner/name` from the cwd's origin, or None when it cannot be read.

    Used ONLY when --repo is absent, and the caller must NAME the inferred value:
    a tool that quietly gates against the cwd repo is the #2502 / #2506 class."""
    git = git or pg._git
    try:
        return pg._nwo_from_url(git(["remote", "get-url", "origin"], cwd=cwd))
    except Exception:  # noqa: BLE001 -- not a repo, no origin, no git: all mean "cannot infer"
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ci_wait.py",
        description="Wait for CI on a PR or SHA over REST only; one line out, four exit codes.",
        epilog=EXIT_CODES_TEXT, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int, help="PR number; its head SHA is re-resolved every poll")
    target.add_argument("--sha", help="commit SHA to watch (pinned; use --pr if the head may move)")
    ap.add_argument("--repo", default=None,
                    help="owner/name (default: the cwd origin, NAMED on stderr; unresolvable -> exit 2)")
    ap.add_argument("--required", default=None,
                    help="only this check name (substring, case-insensitive); default: every "
                         "check-run, and GREEN waits for one from the repo's required workflow")
    ap.add_argument("--interval", type=float, default=INTERVAL_DEFAULT,
                    help="seconds between polls (default %g; floor %g)" % (INTERVAL_DEFAULT, INTERVAL_FLOOR))
    ap.add_argument("--deadline", type=float, default=DEADLINE_DEFAULT,
                    help="seconds before TIMEOUT / UNDETERMINED (default %g)" % DEADLINE_DEFAULT)
    ap.add_argument("--gh", default="gh", help="gh binary (test seam)")
    ap.add_argument("--json", action="store_true", help="emit the verdict dict instead of the line")
    a = ap.parse_args(argv)

    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

    repo = a.repo
    if not repo:
        repo = infer_repo()
        if not repo:
            v = _verdict("UNDETERMINED", UNDETERMINED, repo=None, pr=a.pr, sha=a.sha,
                         required=a.required, c=None, polls=0, failures=0, elapsed=0.0,
                         error="repo unresolvable: no --repo and the cwd has no readable origin "
                               "-- pass --repo owner/name (guessing here is the #2502/#2506 wrong-repo class)")
            print(json.dumps(v) if a.json else format_line(v))
            return UNDETERMINED
        _stderr("ci_wait: repo inferred from the cwd origin: %s (pass --repo to pin it)" % repo)

    v = wait(repo=repo, pr=a.pr, sha=a.sha, required=a.required,
             workflow=None if a.required else AUTO,
             interval=a.interval, deadline=a.deadline, gh=a.gh)
    print(json.dumps(v) if a.json else format_line(v))
    return int(v["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
