"""#2549 (b) -- one shared, REST-only, deadline-bound CI waiter.

Four sessions in two days hand-rolled a CI wait and three of them exhausted the
account's shared GitHub GraphQL quota doing it (`gh pr checks --watch`, or
`gh pr view --json statusCheckRollup` every 20s). The fourth instance was
solo-inline with no lanes at all, so "the orchestrator owns the watch" cannot be
the fix; a tool every session reaches for can.

Properties pinned here, each one a burn:

  REST ONLY        `repos/{repo}/commits/{sha}/check-runs` -- never `gh pr view`.
  RE-RESOLVE HEAD  `--pr` re-reads the head SHA every poll; an `update-branch`
                   mid-wait moves it, and a SHA-pinned watcher waits forever.
  NEWEST WINS      check-runs keep superseded runs; a CANCELLED `labels` beside
                   a later SUCCESS `labels` is green, not red (2026-09-05).
  FOUR VERDICTS    GREEN / RED / UNDETERMINED / TIMEOUT. An API failure is never
                   TIMEOUT, a slow CI is never UNDETERMINED, and neither is GREEN.
  NULL IS NULL     zero check-runs for the whole deadline is "nothing reported",
                   never "nothing failed".
  FLOOR            interval < 30s is raised, never honoured; 30s is also the
                   default (#3303).
  RUNNER DIED      a run GitHub concludes `failure` because its runner went away
                   mid-step is a cancel, not a red (#2937, 2026-10-01).
  REQUIRED WF      by default (the CLI) GREEN waits for a check-run from the repo's
                   required workflow; a fast `labels` run finishing first is not CI
                   (#2987, PR #2985: GREEN in 2s, 110s before `agent-os tests` existed).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_wait as cw  # noqa: E402

REPO = "octo/home-repo"
SHA1 = "eb6313f4aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _no_plugin_option(monkeypatch):
    """The plugin option must come from the test, never the runner's environment."""
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_REQUIRED_CHECK", raising=False)
SHA2 = "2845ded7bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


# --- fixtures: the REST shapes, verbatim from a live call on 2845ded7 -----------

def run(name, conclusion, status="completed", rid=None, url=None):
    """One `check_runs[]` entry as `repos/.../check-runs` returns it."""
    return {
        "id": rid if rid is not None else abs(hash((name, conclusion, status))) % 10**9,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "details_url": url or f"https://github.com/{REPO}/actions/runs/1/job/{name}",
        "html_url": url or f"https://github.com/{REPO}/actions/runs/1/job/{name}",
        "started_at": "2026-09-05T10:21:47Z",
        "completed_at": "2026-09-05T10:29:14Z" if status == "completed" else None,
        "check_suite": {"id": 92031238777},
        "app": {"slug": "github-actions"},
    }


def runs(*entries):
    return {"total_count": len(entries), "check_runs": list(entries)}


def pull(sha, state="open", merged=False):
    return {"number": 2575, "state": state, "merged": merged, "head": {"sha": sha}}


CANCELLED_MSG = "The operation was canceled."


def ann(level, message, path=".github", line=133, title=""):
    """One `check-runs/{id}/annotations` entry, shaped like the live payloads."""
    return {"path": path, "start_line": line, "end_line": line, "start_column": None,
            "end_column": None, "annotation_level": level, "title": title,
            "message": message, "raw_details": ""}


# Verbatim from job 110242831485 (main 59ba32aa, 2026-10-01): an unattended-upgrade
# restarted the runner mid-step; this is the run's ONLY annotation.
ABANDONED = [ann("failure", CANCELLED_MSG)]
# Job 109293591087 (main 37e429fd, 2026-09-29), a genuine test failure: the exit-code
# annotation plus a warning-level budget note (one of the four it carried).
GENUINE = [ann("failure", "Process completed with exit code 2.", line=7383),
           ann("warning", "[at-pin] skills/ingest/SKILL.md 25,680 B has 256 B headroom "
               "against its 25,936 B budget", path="skills/ingest/SKILL.md", line=0,
               title="SKILL.md near budget")]


def annotations_url(rid):
    return f"repos/{REPO}/check-runs/{rid}/annotations?per_page={cw.ANNOTATIONS_PAGE}"


class FakeGH:
    """Scripted `gh_json(gh, args)`: each call pops the next item -- a payload to
    return or an Exception to raise -- and records the args it was asked for."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, gh, args):
        self.calls.append(list(args))
        if not self.script:
            raise AssertionError(f"FakeGH exhausted; extra call: {args}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def urls(self):
        return [a[-1] for a in self.calls if a and a[0] == "api"]


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _wait(gh, clock=None, **kw):
    clock = clock or Clock()
    kw.setdefault("repo", REPO)
    kw.setdefault("interval", 60)
    kw.setdefault("deadline", 600)
    kw.setdefault("log", lambda *_a, **_k: None)
    return cw.wait(gh_json=gh, sleep=clock.sleep, now=clock.now, **kw), clock


# --- GREEN -----------------------------------------------------------------------

def test_green_when_every_latest_run_is_success_or_skipped():
    gh = FakeGH(runs(run("test", "success"), run("advance-deploy-ref", "skipped")))
    v, clock = _wait(gh, sha=SHA1)
    assert v["verdict"] == "GREEN" and v["exit_code"] == cw.GREEN
    assert v["polls"] == 1 and clock.sleeps == []
    assert {c["name"] for c in v["checks"]} == {"test", "advance-deploy-ref"}


def test_uses_the_rest_check_runs_endpoint_and_never_gh_pr_view():
    gh = FakeGH(runs(run("test", "success")))
    _wait(gh, sha=SHA1)
    assert gh.calls == [["api", f"repos/{REPO}/commits/{SHA1}/check-runs"]]
    assert not any(a[:2] == ["pr", "view"] for a in gh.calls), "GraphQL is the quota burner"


def test_pending_then_success_polls_at_the_interval():
    gh = FakeGH(runs(run("test", None, status="in_progress")),
                runs(run("test", "success")))
    v, clock = _wait(gh, sha=SHA1, interval=60)
    assert v["verdict"] == "GREEN" and v["polls"] == 2
    assert clock.sleeps == [60]


def test_superseded_cancelled_run_loses_to_the_newer_success_of_the_same_name():
    # 2026-09-05 PR #2575: `labels` CANCELLED (superseded) + `labels` SUCCESS.
    # The rollup shows both; only the newest per name is a verdict.
    gh = FakeGH(runs(run("labels", "cancelled", rid=100),
                     run("labels", "success", rid=200),
                     run("test", "success", rid=300)))
    v, _ = _wait(gh, sha=SHA1)
    assert v["verdict"] == "GREEN", v
    assert [c for c in v["checks"] if c["name"] == "labels"][0]["conclusion"] == "success"


def test_newest_is_by_id_not_by_list_order():
    gh = FakeGH(runs(run("labels", "success", rid=200),
                     run("labels", "cancelled", rid=100)))
    v, _ = _wait(gh, sha=SHA1)
    assert v["verdict"] == "GREEN"


# --- RED --------------------------------------------------------------------------

def test_red_immediately_on_a_confirmed_failure_and_names_the_job_and_url():
    gh = FakeGH(runs(run("test", "failure", url="https://x/run/9"),
                     run("labels", "success")), GENUINE)
    v, clock = _wait(gh, sha=SHA1)
    assert v["verdict"] == "RED" and v["exit_code"] == cw.RED
    assert clock.sleeps == [], "a confirmed red does not wait out the deadline"
    assert v["failing"] == [{"name": "test", "conclusion": "failure", "url": "https://x/run/9"}]
    line = cw.format_line(v)
    assert "CI RED" in line and "test:failure" in line and "https://x/run/9" in line


def test_timed_out_and_action_required_are_red_not_pending():
    for concl in ("timed_out", "action_required", "startup_failure"):
        gh = FakeGH(runs(run("test", concl)))
        v, _ = _wait(gh, sha=SHA1)
        assert v["verdict"] == "RED", concl


def test_red_wins_over_a_sibling_still_pending():
    gh = FakeGH(runs(run("test", "failure"), run("labels", None, status="queued")), GENUINE)
    v, _ = _wait(gh, sha=SHA1)
    assert v["verdict"] == "RED"


# --- UNDETERMINED: the instrument, not the world ----------------------------------

def test_api_failure_every_poll_is_undetermined_never_timeout_never_green():
    err = RuntimeError("gh api ... failed: API rate limit already exceeded for user ID 33196704.")
    gh = FakeGH(*[RuntimeError(str(err))] * 50)
    v, clock = _wait(gh, sha=SHA1, interval=60, deadline=300)
    assert v["verdict"] == "UNDETERMINED" and v["exit_code"] == cw.UNDETERMINED
    assert "rate limit" in v["error"]
    assert v["instrument_failures"] == v["polls"] >= 2
    assert clock.t >= 300


def test_rate_limit_text_backs_off_the_interval_instead_of_hammering():
    gh = FakeGH(*[RuntimeError("API rate limit already exceeded")] * 50)
    _, clock = _wait(gh, sha=SHA1, interval=60, deadline=1000)
    assert clock.sleeps[0] == 60
    assert clock.sleeps[1] > clock.sleeps[0], "no back-off on a rate-limit error"
    assert max(clock.sleeps) <= cw.BACKOFF_MAX


def test_a_generic_error_does_not_back_off():
    gh = FakeGH(*[RuntimeError("connection reset")] * 50)
    _, clock = _wait(gh, sha=SHA1, interval=60, deadline=300)
    assert set(clock.sleeps) == {60}


def test_one_failed_poll_between_good_ones_is_retried_not_terminal():
    gh = FakeGH(runs(run("test", None, status="in_progress")),
                RuntimeError("502 Bad Gateway"),
                runs(run("test", "success")))
    v, _ = _wait(gh, sha=SHA1)
    assert v["verdict"] == "GREEN" and v["instrument_failures"] == 1 and v["polls"] == 3


def test_cancelled_with_no_successor_is_undetermined_at_the_deadline_not_red():
    # A cancel means "did not finish": no verdict about the code either way.
    gh = FakeGH(*[runs(run("labels", "cancelled"), run("test", "success"))] * 50)
    v, _ = _wait(gh, sha=SHA1, interval=60, deadline=180)
    assert v["verdict"] == "UNDETERMINED"
    assert "labels" in cw.format_line(v) and "cancelled" in cw.format_line(v)


# --- a runner that died mid-job is a cancel, not a red (#2937) --------------------------
# 2026-10-01: an unattended-upgrade restarted cx33-runner mid-step. GitHub cancelled the
# step but concluded the CHECK RUN `failure`, so ci_wait printed RED on main with no
# test behind it. The check run's annotations (REST) tell the two apart.

def test_a_runner_abandoned_failure_reads_cancelled_not_red():
    gh = FakeGH(runs(run("test", "failure", rid=110242831485)), ABANDONED,
                *[runs(run("test", "failure", rid=110242831485))] * 50)
    v, clock = _wait(gh, sha=SHA1, interval=60, deadline=180)
    assert v["verdict"] == "UNDETERMINED" and v["exit_code"] == cw.UNDETERMINED, v
    assert v["state"] == "cancelled" and v["failing"] == []
    assert clock.t >= 180, "a cancel waits for a re-run until the deadline, like any cancel"
    line = cw.format_line(v)
    assert "cancelled=test" in line and "no verdict about the code" in line


def test_the_genuine_failure_annotation_stays_red_at_once():
    gh = FakeGH(runs(run("test", "failure", rid=109293591087)), GENUINE)
    v, clock = _wait(gh, sha=SHA1)
    assert v["verdict"] == "RED" and clock.sleeps == []
    assert [f["name"] for f in v["failing"]] == ["test"]


def test_warnings_beside_the_cancel_message_do_not_make_it_red():
    gh = FakeGH(runs(run("test", "failure", rid=7)), ABANDONED + GENUINE[1:],
                *[runs(run("test", "failure", rid=7))] * 50)
    v, _ = _wait(gh, sha=SHA1, interval=60, deadline=120)
    assert v["verdict"] == "UNDETERMINED", v


def test_any_other_failure_annotation_beside_the_cancel_keeps_it_red():
    # A timeout or a lost runner adds its own failure annotation beside the cancel.
    timeout = ann("failure", "The job running on runner cx33-runner has exceeded the "
                             "maximum execution time of 30 minutes.")
    gh = FakeGH(runs(run("test", "failure", rid=8)), ABANDONED + [timeout])
    v, _ = _wait(gh, sha=SHA1)
    assert v["verdict"] == "RED"


def test_no_failure_annotation_at_all_is_no_evidence_of_a_cancel():
    gh = FakeGH(runs(run("test", "failure", rid=9)), GENUINE[1:])
    v, _ = _wait(gh, sha=SHA1)
    assert v["verdict"] == "RED"


def test_an_annotation_read_that_fails_leaves_the_run_red():
    notes = []
    gh = FakeGH(runs(run("test", "failure", rid=10)), RuntimeError("502 Bad Gateway"))
    v, clock = _wait(gh, sha=SHA1, log=notes.append)
    assert v["verdict"] == "RED" and clock.sleeps == []
    assert any("unreadable" in n for n in notes), notes


def test_a_full_annotation_page_is_not_read_as_a_cancel():
    page = ABANDONED + [ann("warning", "w%d" % i) for i in range(cw.ANNOTATIONS_PAGE - 1)]
    gh = FakeGH(runs(run("test", "failure", rid=11)), page)
    v, _ = _wait(gh, sha=SHA1)
    assert v["verdict"] == "RED", "a full page can hide a later failure annotation"


def test_annotations_are_read_over_rest_once_per_run_id():
    gh = FakeGH(runs(run("test", "failure", rid=12)), ABANDONED,
                *[runs(run("test", "failure", rid=12))] * 50)
    _wait(gh, sha=SHA1, interval=60, deadline=240)
    assert [u for u in gh.urls() if "/annotations" in u] == [annotations_url(12)]
    assert all(a[0] == "api" for a in gh.calls), "REST only: never gh pr view / checks"


def test_only_a_failure_conclusion_is_inspected():
    for concl in ("timed_out", "action_required", "startup_failure"):
        gh = FakeGH(runs(run("test", concl, rid=13)))
        v, _ = _wait(gh, sha=SHA1)
        assert v["verdict"] == "RED", concl
        assert not any("/annotations" in u for u in gh.urls()), concl


def test_required_narrows_which_failures_are_inspected():
    gh = FakeGH(runs(run("test", "success"), run("labels", "failure", rid=15)))
    v, _ = _wait(gh, sha=SHA1, required="test")
    assert v["verdict"] == "GREEN"
    assert not any("/annotations" in u for u in gh.urls())


def test_a_rerun_after_an_abandoned_run_is_followed_to_its_verdict():
    gh = FakeGH(runs(run("test", "failure", rid=100)), ABANDONED,
                runs(run("test", "failure", rid=100), run("test", "success", rid=200)))
    v, _ = _wait(gh, sha=SHA1, interval=60, deadline=600)
    assert v["verdict"] == "GREEN" and v["polls"] == 2


def test_the_abandoned_run_is_named_on_stderr_once():
    notes = []
    gh = FakeGH(runs(run("test", "failure", rid=14)), ABANDONED,
                *[runs(run("test", "failure", rid=14))] * 50)
    _wait(gh, sha=SHA1, interval=60, deadline=180, log=notes.append)
    hits = [n for n in notes if "abandoned" in n]
    assert len(hits) == 1 and "test" in hits[0] and "#2937" in hits[0], notes


def test_runner_abandoned_needs_a_list_of_annotations():
    assert cw.runner_abandoned(ABANDONED) is True
    assert cw.runner_abandoned(None) is False
    assert cw.runner_abandoned({"annotation_level": "failure", "message": CANCELLED_MSG}) is False
    assert cw.runner_abandoned([]) is False


# --- TIMEOUT: the world was slow, the instrument was fine ----------------------------

def test_still_pending_at_the_deadline_is_timeout():
    gh = FakeGH(*[runs(run("test", None, status="in_progress"))] * 50)
    v, clock = _wait(gh, sha=SHA1, interval=60, deadline=180)
    assert v["verdict"] == "TIMEOUT" and v["exit_code"] == cw.TIMEOUT
    assert v["state"] == "pending" and clock.t >= 180


def test_zero_check_runs_for_the_whole_deadline_is_timeout_and_says_none_appeared():
    gh = FakeGH(*[runs()] * 50)
    v, _ = _wait(gh, sha=SHA1, interval=60, deadline=180)
    assert v["verdict"] == "TIMEOUT" and v["checks_seen"] == 0
    line = cw.format_line(v)
    assert "checks_seen=0" in line and "no check-runs" in line


def test_only_skipped_runs_never_count_as_green():
    gh = FakeGH(*[runs(run("advance-deploy-ref", "skipped"))] * 50)
    v, _ = _wait(gh, sha=SHA1, interval=60, deadline=180)
    assert v["verdict"] == "TIMEOUT" and v["state"] == "skipped_only"


def test_sleep_never_overshoots_the_deadline():
    gh = FakeGH(*[runs(run("test", None, status="queued"))] * 50)
    _, clock = _wait(gh, sha=SHA1, interval=60, deadline=100)
    assert clock.sleeps == [60, 40]


# --- --pr: the head can move under you ------------------------------------------------

def test_pr_mode_resolves_head_over_rest_then_reads_that_shas_check_runs():
    gh = FakeGH(pull(SHA1), runs(run("test", "success")))
    v, _ = _wait(gh, pr=2575)
    assert v["verdict"] == "GREEN" and v["sha"] == SHA1
    assert gh.calls == [["api", f"repos/{REPO}/pulls/2575"],
                        ["api", f"repos/{REPO}/commits/{SHA1}/check-runs"]]


def test_pr_mode_re_resolves_the_head_every_poll_so_update_branch_is_followed():
    gh = FakeGH(pull(SHA1), runs(run("test", None, status="in_progress")),
                pull(SHA2), runs(run("test", "success")))
    v, _ = _wait(gh, pr=2575)
    assert v["verdict"] == "GREEN" and v["sha"] == SHA2
    assert gh.urls()[-1] == f"repos/{REPO}/commits/{SHA2}/check-runs"


def test_pr_and_sha_together_is_a_usage_error():
    with pytest.raises(ValueError):
        cw.wait(repo=REPO, pr=1, sha=SHA1, gh_json=FakeGH())


# --- --required narrows the set ----------------------------------------------------------

def test_required_filters_to_the_named_check_and_ignores_the_rest():
    gh = FakeGH(runs(run("test", "success"), run("labels", "failure")))
    v, _ = _wait(gh, sha=SHA1, required="test")
    assert v["verdict"] == "GREEN"
    assert [c["name"] for c in v["checks"]] == ["test"]


def test_required_absent_for_the_whole_deadline_is_timeout_not_green():
    gh = FakeGH(*[runs(run("labels", "success"))] * 50)
    v, _ = _wait(gh, sha=SHA1, required="test", interval=60, deadline=120)
    assert v["verdict"] == "TIMEOUT" and v["checks_seen"] == 0


# --- #2987: GREEN waits for the repo's required workflow ----------------------------------
#
# PR #2985, 2026-10-06: `ci_wait.py --pr 2985` printed `CI GREEN ... checks=labels:success
# elapsed=2s polls=1`. The `agent-os tests` run on that head was created ~110 s later, so
# the GREEN covered none of the test suite. REST check-runs carry JOB names only; the
# workflow a run belongs to comes from `actions/runs?head_sha=<40 chars>`, joined on the
# Actions run id in each check-run's `details_url`.

WF = "agent-os tests"
LABELS_RID, TESTS_RID, PROBE_RID = 37478800001, 37478854757, 37478800002


def wrun(name, conclusion, run_id, status="completed", rid=None, head_sha=SHA1):
    """A check-run whose `details_url` names Actions run `run_id`, as the live ones do."""
    r = run(name, conclusion, status=status, rid=rid,
            url=f"https://github.com/{REPO}/actions/runs/{run_id}/job/{abs(hash(name)) % 10**9}")
    r["head_sha"] = head_sha
    return r


def wf_runs(*pairs):
    """`actions/runs?head_sha=` payload: (run id, workflow name) pairs."""
    return {"total_count": len(pairs),
            "workflow_runs": [{"id": rid, "name": name, "head_sha": SHA1} for rid, name in pairs]}


def workflows(*names):
    """`actions/workflows` payload."""
    return {"total_count": len(names),
            "workflows": [{"id": i, "name": n, "path": f".github/workflows/w{i}.yml",
                           "state": "active"} for i, n in enumerate(names)]}


def join_url(sha=SHA1):
    return f"repos/{REPO}/actions/runs?head_sha={sha}&per_page=100"


LABELS_ONLY = runs(wrun("labels", "success", LABELS_RID))
LABELS_JOIN = wf_runs((LABELS_RID, "PR label guard"))
BOTH_GREEN = runs(wrun("labels", "success", LABELS_RID), wrun("test", "success", TESTS_RID))
BOTH_JOIN = wf_runs((LABELS_RID, "PR label guard"), (TESTS_RID, WF))
CDE_WORKFLOWS = workflows(WF, "PR label guard", "visual-verify capability probe",
                          "visual-verify gate", "visual-verify sizing")


def test_a_labels_only_green_is_not_ci_while_the_required_workflow_is_absent():
    gh = FakeGH(*[LABELS_ONLY, LABELS_JOIN] * 10)
    v, _ = _wait(gh, sha=SHA1, workflow=WF, interval=60, deadline=120)
    assert v["verdict"] == "TIMEOUT" and v["exit_code"] == cw.TIMEOUT
    assert v["state"] == "awaiting_workflow" and v["workflow"] == WF


def test_control_the_same_payload_plus_the_required_workflows_run_is_green():
    gh = FakeGH(BOTH_GREEN, BOTH_JOIN)
    v, _ = _wait(gh, sha=SHA1, workflow=WF)
    assert v["verdict"] == "GREEN" and v["polls"] == 1
    assert gh.urls() == [f"repos/{REPO}/commits/{SHA1}/check-runs", join_url()]


def test_the_2985_sequence_waits_through_registration_to_the_real_verdict():
    # labels green alone -> tests registered and running -> tests green.
    gh = FakeGH(LABELS_ONLY, LABELS_JOIN,
                runs(wrun("labels", "success", LABELS_RID),
                     wrun("test", None, TESTS_RID, status="in_progress")),
                BOTH_GREEN, BOTH_JOIN)
    v, clock = _wait(gh, sha=SHA1, workflow=WF, interval=60)
    assert v["verdict"] == "GREEN" and v["polls"] == 3 and clock.sleeps == [60, 60]


def test_a_failure_is_red_at_once_even_before_the_required_workflow_registers():
    gh = FakeGH(runs(wrun("labels", "failure", LABELS_RID, rid=808441694)), GENUINE)
    v, _ = _wait(gh, sha=SHA1, workflow=cw.AUTO)
    assert v["verdict"] == "RED"
    assert not any("actions/" in u for u in gh.urls()), "a RED needs no workflow probe or join"


def test_a_failed_join_is_undetermined_never_timeout_never_green():
    gh = FakeGH(*[LABELS_ONLY, RuntimeError("HTTP 502")] * 10)
    v, _ = _wait(gh, sha=SHA1, workflow=WF, interval=60, deadline=120)
    assert v["verdict"] == "UNDETERMINED" and v["instrument_failures"] >= 1


def test_a_short_sha_joins_on_the_check_runs_full_head_sha():
    # actions/runs?head_sha=<8 chars> answers total_count 0 with no error (measured
    # 2026-09-05, premerge_guard #2583): the join must use the 40-char form.
    gh = FakeGH(BOTH_GREEN, BOTH_JOIN)
    v, _ = _wait(gh, sha=SHA1[:8], workflow=WF)
    assert v["verdict"] == "GREEN"
    assert gh.urls()[-1] == join_url(SHA1)


def test_auto_resolves_the_required_workflow_once_over_rest():
    gh = FakeGH(LABELS_ONLY, workflows(WF), LABELS_JOIN, BOTH_GREEN, BOTH_JOIN)
    v, _ = _wait(gh, sha=SHA1, workflow=cw.AUTO, interval=60)
    assert v["verdict"] == "GREEN" and v["workflow"] == WF
    assert gh.urls().count(f"repos/{REPO}/actions/workflows") == 1


def test_auto_takes_the_plugin_option_without_a_probe(monkeypatch):
    """G4: the `required_check` option names the workflow; no derive is needed."""
    monkeypatch.setenv(cw.pg.REQUIRED_CHECK_ENV, WF)
    gh = FakeGH(BOTH_GREEN, BOTH_JOIN)
    v, _ = _wait(gh, sha=SHA1, workflow=cw.AUTO)
    assert v["verdict"] == "GREEN" and v["workflow"] == WF
    assert not any("actions/workflows" in u for u in gh.urls())


def test_auto_on_a_many_workflow_repo_without_option_has_no_required_workflow(monkeypatch):
    """No workflow name is special in the kit: five workflows and no option
    resolve to none, said on stderr, with the option named as the fix."""
    notes = []
    gh = FakeGH(LABELS_ONLY, CDE_WORKFLOWS)
    v, _ = _wait(gh, sha=SHA1, workflow=cw.AUTO, log=notes.append)
    assert v["workflow"] is None
    assert any("required_check" in n for n in notes)


def test_auto_on_a_one_workflow_repo_waits_for_that_workflow():
    # OtherRepo: one workflow, named `ci`.
    gh = FakeGH(*[LABELS_ONLY, workflows("ci"), LABELS_JOIN] + [LABELS_ONLY, LABELS_JOIN] * 10)
    v, _ = _wait(gh, sha=SHA1, workflow=cw.AUTO, interval=60, deadline=60)
    assert v["verdict"] == "TIMEOUT" and v["workflow"] == "ci"


def test_auto_with_no_resolvable_workflow_says_so_and_keeps_the_present_runs_verdict():
    notes = []
    gh = FakeGH(LABELS_ONLY, workflows("build", "lint"))
    v, _ = _wait(gh, sha=SHA1, workflow=cw.AUTO, log=notes.append)
    assert v["verdict"] == "GREEN" and v["workflow"] is None
    assert any("--required" in n and "only the check-runs present" in n for n in notes)


def test_auto_whose_workflows_probe_fails_is_retried_not_guessed():
    gh = FakeGH(*[LABELS_ONLY, RuntimeError("HTTP 500")] * 10)
    v, _ = _wait(gh, sha=SHA1, workflow=cw.AUTO, interval=60, deadline=120)
    assert v["verdict"] == "UNDETERMINED" and v["workflow"] is None


def test_the_timeout_line_names_the_workflow_it_waited_for():
    gh = FakeGH(*[LABELS_ONLY, LABELS_JOIN] * 10)
    v, _ = _wait(gh, sha=SHA1, workflow=WF, interval=60, deadline=60)
    line = cw.format_line(v)
    line.encode("ascii")
    assert "state=awaiting_workflow" in line and "'agent-os tests'" in line


def test_main_turns_the_workflow_gate_on_by_default_and_off_under_required(monkeypatch):
    seen = []

    def fake_wait(**kw):
        seen.append(kw.get("workflow"))
        return {"verdict": "GREEN", "exit_code": 0, "repo": REPO, "pr": None, "sha": SHA1,
                "state": "green", "checks": [], "failing": [], "polls": 1,
                "instrument_failures": 0, "error": None, "elapsed_s": 0, "required": None,
                "checks_seen": 0, "workflow": None}
    monkeypatch.setattr(cw, "wait", fake_wait)
    cw.main(["--sha", SHA1, "--repo", REPO])
    cw.main(["--sha", SHA1, "--repo", REPO, "--required", "test"])
    assert seen[0] is cw.AUTO and seen[1] is None


def test_main_end_to_end_reads_the_2985_payload_as_not_green(capsys, monkeypatch):
    # The running configuration: the CLI, its real wait(), only the REST transport faked.
    # The repo has five workflows, so the option names the one to wait for (G4).
    monkeypatch.setenv(cw.pg.REQUIRED_CHECK_ENV, WF)
    gh = FakeGH(LABELS_ONLY, LABELS_JOIN)
    monkeypatch.setattr(cw.pg, "_gh_json", gh)
    rc = cw.main(["--sha", SHA1, "--repo", REPO, "--deadline", "0"])
    out = capsys.readouterr().out
    assert rc == cw.TIMEOUT and out.startswith("CI TIMEOUT") and "awaiting_workflow" in out


# --- interval floor ------------------------------------------------------------------------

def test_interval_below_the_floor_is_raised_to_it_and_said_so():
    gh = FakeGH(runs(run("test", None, status="queued")), runs(run("test", "success")))
    notes = []
    v, clock = _wait(gh, sha=SHA1, interval=5, log=lambda m: notes.append(m))
    assert clock.sleeps == [cw.INTERVAL_FLOOR]
    assert any("floor" in n for n in notes)


def test_the_default_interval_is_the_30s_floor():
    # #3303: a 60s default noticed a finished run ~30s late on a ~8 min CI wait. A pending
    # --pr poll is 2 REST calls (pull head + check-runs), so 30s costs ~32 calls per wait.
    gh = FakeGH(runs(run("test", None, status="in_progress")), runs(run("test", "success")))
    clock = Clock()
    v = cw.wait(repo=REPO, sha=SHA1, gh_json=gh, sleep=clock.sleep, now=clock.now,
                log=lambda *_a, **_k: None)
    assert v["verdict"] == "GREEN" and v["polls"] == 2
    assert clock.sleeps == [30]
    assert cw.INTERVAL_DEFAULT == cw.INTERVAL_FLOOR == 30


def test_main_passes_the_30s_default_interval(monkeypatch):
    seen = []

    def fake_wait(**kw):
        seen.append(kw.get("interval"))
        return {"verdict": "GREEN", "exit_code": 0, "repo": REPO, "pr": None, "sha": SHA1,
                "state": "green", "checks": [], "failing": [], "polls": 1,
                "instrument_failures": 0, "error": None, "elapsed_s": 0, "required": None,
                "checks_seen": 0, "workflow": None}
    monkeypatch.setattr(cw, "wait", fake_wait)
    cw.main(["--sha", SHA1, "--repo", REPO])
    assert seen == [30]


# --- output contract -----------------------------------------------------------------------

def test_final_line_is_one_ascii_line_led_by_the_verdict():
    gh = FakeGH(runs(run("test", "success"), run("labels", "success")))
    v, _ = _wait(gh, sha=SHA1, pr=None)
    line = cw.format_line(v)
    assert "\n" not in line
    line.encode("ascii")
    assert line.startswith("CI GREEN ")
    assert f"sha={SHA1[:8]}" in line and "polls=1" in line


def test_non_ascii_in_an_error_is_escaped_not_crashed():
    gh = FakeGH(*[RuntimeError("café — down")] * 50)
    v, _ = _wait(gh, sha=SHA1, interval=60, deadline=60)
    cw.format_line(v).encode("ascii")


# --- repo resolution: never silently the cwd repo (#2502 / #2506 class) --------------------

def test_infer_repo_reads_the_cwd_origin_and_names_it():
    got = cw.infer_repo(git=lambda args, cwd=None: "https://github.com/octo/OtherRepo.git")
    assert got == "octo/OtherRepo"


def test_infer_repo_returns_none_when_git_cannot_answer():
    def boom(args, cwd=None):
        raise RuntimeError("not a git repo")
    assert cw.infer_repo(git=boom) is None


def test_main_without_repo_and_no_origin_exits_undetermined_not_a_guess(capsys, monkeypatch):
    monkeypatch.setattr(cw, "infer_repo", lambda cwd=None, git=None: None)
    rc = cw.main(["--sha", SHA1])
    assert rc == cw.UNDETERMINED
    out = capsys.readouterr().out
    assert out.startswith("CI UNDETERMINED") and "--repo" in out


def test_main_prints_the_inferred_repo_to_stderr(capsys, monkeypatch):
    monkeypatch.setattr(cw, "infer_repo", lambda cwd=None, git=None: REPO)
    monkeypatch.setattr(cw, "wait", lambda **kw: {
        "verdict": "GREEN", "exit_code": 0, "repo": kw["repo"], "pr": None, "sha": SHA1,
        "state": "green", "checks": [], "failing": [], "polls": 1, "instrument_failures": 0,
        "error": None, "elapsed_s": 0, "required": None, "checks_seen": 0})
    rc = cw.main(["--sha", SHA1])
    assert rc == 0
    err = capsys.readouterr().err
    assert "inferred" in err and REPO in err


def test_main_json_emits_the_verdict_dict(capsys, monkeypatch):
    monkeypatch.setattr(cw, "wait", lambda **kw: {
        "verdict": "RED", "exit_code": 1, "repo": REPO, "pr": 7, "sha": SHA1,
        "state": "red", "checks": [], "failing": [{"name": "test", "conclusion": "failure", "url": "u"}],
        "polls": 1, "instrument_failures": 0, "error": None, "elapsed_s": 3, "required": None,
        "checks_seen": 1})
    rc = cw.main(["--pr", "7", "--repo", REPO, "--json"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "RED"


def test_cli_help_runs_as_a_script():
    r = subprocess.run([sys.executable, str(Path(cw.__file__)), "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "--pr" in r.stdout and "--sha" in r.stdout and "exit" in r.stdout.lower()
