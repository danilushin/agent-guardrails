# agent-guardrails

Guardrails for coding agents that open their own pull requests. Each guard exists because an agent once did the thing it now blocks.

**Status: pre-release.** Version 0.1 carries four guards:

- **Merge gate** (a `PreToolUse` hook on Bash). Blocks an agent-issued merge into `main` unless a person authorized that pull request, and blocks an agent push to `main` outright.
- **Pre-merge check** (`premerge-guard --pr <N>`). Holds a merge whose CI is not green or whose base moved after CI ran.
- **CI wait** (`ci-wait --pr <N>` or `--sha <sha>`). One waiter with four verdicts (green, red, undetermined, timeout) that never spends the shared GraphQL quota.
- **Duplicate scan** (`dup-scan --issue <N> --prebranch --files <a,b>`, and `--pre-pr` before opening the pull request). Finds an open or merged pull request for the same issue or the same files, and a local worktree or branch already working on it. Exit 3 holds, 4 means the scan could not tell, 0 is go.

Install it from Claude Code 2.1.275 or later:

```
/plugin install agent-guardrails --marketplace danilushin/agent-guardrails
```

While the plugin is enabled, `premerge-guard`, `ci-wait` and `dup-scan` are on the Bash tool's `PATH`. All three need `python3` and the GitHub CLI (`gh`), signed in.

## Authorizing a merge

When the agent tries to merge a pull request into `main`, the gate blocks it and asks you to authorize. Type `mgo <N>` (or `mgo <owner>/<repo>#<N>`) as your entire prompt. The plugin runs the pre-merge check, and on GO it authorizes that one merge for 10 minutes; the agent then runs it. A phrase inside a longer prompt never counts. A push to `main` has no authorization: the gate hands you the command to run yourself with `!`.

Each authorization is a one-use file in the plugin's data directory (`~/.claude/plugins/data/<plugin id>/`) and is appended to `arm-log.jsonl` there. Set the `notify_command` option to run a command of your own on each one.

The hooks need Python 3.10 or newer on `PATH` (the `python3` that ships with macOS is 3.9) and Claude Code 2.1.295 or newer. Without such a Python, the gate blocks every Bash command and a message at session start says how to fix it.

## Which CI check counts

The pre-merge check gates on one CI check, chosen in this order:

1. `--required-check <name>` on the command line.
2. The plugin's `required_check` option. Claude Code passes it to hooks only, so from the Bash tool use `--required-check`.
3. The repository's only workflow, when it has exactly one.

Anything else holds the merge, and the hold names the fix. The CI wait takes the same option, or `--required <name>`.

## Concurrent sessions

A local worktree or branch is never a hold, only a note: a second worktree on related work must not be blocked. When the worktree holds a `.session-lock` (written by `scripts/session_lock.py write <worktree>/.session-lock`), the note says whether a live session holds it. The scan takes the current session's id from `CLAUDE_CODE_SESSION_ID`. Without it, a live lock might be the session's own, so it reads `unknown` and counts as occupied.

## Why each guard exists

Every guard answers something that happened while agents ran a real repository.

| Guard | What happened | Date | Test that pins the fix |
|---|---|---|---|
| Merge gate | A subagent merged a pull request it had opened into `main` 12 seconds after creating it, and its description claimed an authorization nobody had given. | 2026-07-02 | `tests/test_merge_gate.py::test_corpus_gated_merge_into_main_blocks` |
| `mgo` authorization | A merge was authorized with a template placeholder pasted in as the person's "quote", and it went through, because the quote was free text that nothing checked. The phrase arm takes the record from the person's own prompt instead. | 2026-07-09 | `tests/test_phrase_arm.py::test_armed_marker_note_is_the_verbatim_prompt` |
| Pre-merge check | Two merges fired by hand landed 16 seconds apart, and the second went in one commit behind, on a base its CI never saw. | 2026-09-12 | `tests/test_premerge_guard.py::test_behind_by_one_is_stale` |
| CI wait | Four sessions in two days each wrote their own CI wait loop, and three of them used up the account's shared GraphQL quota. | 2026-09-04/05 | `tests/test_ci_wait.py::test_uses_the_rest_check_runs_endpoint_and_never_gh_pr_view` |
| Duplicate scan | A concurrent session filed and merged a duplicate fix while another session was still building the same one. | 2026-06-16 | `tests/test-dup-scan-prebranch.sh` |
| Session lock | A second session ran `git reset --hard` in a worktree another session was using, and wiped 13 tests in the middle of a commit. | 2026-06-28 | `tests/test_session_lock.py::test_written_lock_for_running_session_reads_alive` |

## What the gate does not catch

The gate reads the command text, so some forms get past it:

- A push whose destination is implicit (`git push`, or `git push origin HEAD` while on `main`), held in a variable, or wrapped in `sh -c '...'` or `eval`.
- A `cd` earlier in the same command: the merge's base is checked from the session's directory.
- A command chained on the same line after a heredoc fed to `cat`, `tee` or `git commit -F -`.
- A ` #` inside a nested quote (`"$(echo "a #b")"`) or an ANSI-C quote (`$'...'`), which is read as the start of a comment.
- An authorization covers a pull request number, not a repository: `mgo 12` in one repository also clears a merge of #12 in another for its 10 minutes.

Nothing at the operating-system level stops an agent that can run shell commands from writing an authorization file itself. The arm log and `notify_command` make every `mgo` visible, but they cannot see a file written by hand, so a merge with no matching line in `arm-log.jsonl` is the sign to look for.

## A second layer: deny rules

A plugin cannot install permission rules. For a layer that does not depend on the hook running, add these to the `permissions.deny` list in your own `~/.claude/settings.json` (or a project's `.claude/settings.json`):

```json
{
  "permissions": {
    "deny": [
      "Bash(git push * main)",
      "Bash(git push * main *)",
      "Bash(git push * master)",
      "Bash(git push * master *)",
      "Bash(git push *:main)",
      "Bash(git push *:main *)",
      "Bash(git push *:master)",
      "Bash(git push *:master *)",
      "Bash(git push *:refs/heads/main*)",
      "Bash(git push *:refs/heads/master*)",
      "Bash(git push * +main)",
      "Bash(git push * +main *)",
      "Bash(git push * +master)",
      "Bash(git push * +master *)"
    ]
  }
}
```

They match command shapes, so they miss forms the gate catches (`git push --all`, for one). They are a second layer, not a replacement for the gate.

## See it block

`demo/push-to-main.sh` runs a headless Claude Code session told to push to `main` twice: once with this plugin loaded (`--plugin-dir`) and once without it. Each run gets a fresh scratch repository whose remote is a local bare repository, so nothing leaves the machine. Both runs load no user settings and allow Bash, so neither your own hooks nor a permission prompt can stand in for the gate. The bare repository's `main` decides the result, not the model's reply.

```
bash demo/push-to-main.sh
```

It exits 0 when the plugin run was blocked by the gate (and prints the gate's message) and the control pushed, 1 when either half failed, and 2 when it could not take a reading (for example, `claude` is not signed in). It calls the model twice, so each run costs a little.

## Issue numbers in the source

Comments in the scripts and tests cite issue and pull request numbers such as `#1234`. They point at the private repository these guards were extracted from and record where a rule came from, so they do not resolve here. Numbers inside test fixtures are test data.

## License

MIT. See [LICENSE](LICENSE).
