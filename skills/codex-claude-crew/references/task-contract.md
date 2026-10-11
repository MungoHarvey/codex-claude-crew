# Task contract

What Codex sends Claude, what comes back, and how Codex checks it.

## The brief

Claude starts with none of the Codex conversation. Write the brief for a capable engineer who just joined the project: the outcome first, then what they need to find their way.

| Section | What to write |
|---|---|
| Goal | The outcome, in one or two sentences. |
| Context | Repo and branch. Files and docs to read first, by path. Rules from `AGENTS.md` that matter (Claude reads `CLAUDE.md` itself, but not `AGENTS.md`). What you already know or ruled out. |
| Scope | Paths to change (implement) or focus on (review). What's off-limits. |
| Acceptance criteria | Statements Codex can check afterwards. |
| Checks | Exact commands. Each one needs a matching `--allow` rule, or Claude's attempt is denied. |
| Limits | Anything the flags don't cover, for example "no new dependencies". |

```text
Goal: ...

Context:
- Repo: <path>, branch <name>
- Read first: <paths>
- Project rules: ...
- Known so far: ...

Scope:
- Change: <paths>        (review: "Focus on: <paths>")
- Don't touch: <paths or behavior>

Acceptance criteria:
- ...

Checks:
- <command>

Limits:
- ...
```

- One task per brief. Split bigger work into several runs.
- Name the files. "Fix the auth bug" sends Claude searching. "`src/auth/session.py` drops the refresh token when the clock skews" doesn't.
- For a review, say what to look for (correctness, security, one specific concern) and what to skip (style, known issues).
- For a follow-up (`--resume`), send only what changed. Claude still has the first brief.

## Claude's report

The relay passes a JSON schema with `--json-schema`, so Claude's final answer comes back as structured data. The relay stores it in `claimed`:

| Field | Meaning |
|---|---|
| `status` | `complete`, `partial` or `blocked`, in Claude's view |
| `summary` | What Claude did or found |
| `findings` | Review issues, each with `severity`, `title`, `file`, `line` and `detail` |
| `changed_files` | Files Claude says it changed |
| `checks` | Commands Claude ran, marked `passed`, `failed`, `not_run` or `denied` |
| `open_issues` | Anything left undone or uncertain |

## The run record

`relay.py` prints the record and saves it as `~/.codex-claude-crew/runs/<run_id>/run.json`, next to the brief and Claude's raw output.

| Field | Meaning |
|---|---|
| `run_id`, `mode`, `cwd` | The run, its mode and the folder Claude worked in |
| `status` | How the Claude process ended: `completed`, `error`, `timed_out`, `interrupted` or `claude_unavailable`. While active, `queued` or `running` |
| `worktree` | Implement runs: `repo`, `path`, `branch` and the `base` commit |
| `request` | What was asked for: model, effort, limits, `--allow` rules, `resume_from` |
| `claude` | `session_id`, `version`, `subtype`, `num_turns`, `cost_usd` (an estimate), `models` used, `permission_denials`, `errors`, `result_text` and the `transcript` path |
| `claimed` | Claude's report (above), or `null` if it didn't return one |
| `observed` | What the relay saw in git: `changed_files`, `commits` added, and `read_only_violation` for review runs |
| `verification` | Codex's verdict. It stays `pending` until `relay.py verify` records `confirmed`, `partial` or `rejected` with the checks Codex ran |
| `notes`, `error` | Warnings, and the failure reason if there was one |

`status: completed` only means Claude finished and returned a result. Whether the task is done is in `claimed.status` (Claude's view) and `verification.verdict` (Codex's).

## Verification

Claude's report is evidence, not proof.

- Compare `claimed.changed_files` with `observed.changed_files`. Any difference needs an explanation.
- Read the whole diff: `git -C <worktree> diff <base>`, plus untracked files from `git -C <worktree> status`.
- Rerun the acceptance checks yourself. A check Claude marked `passed` doesn't count until you've run it.
- Check every review finding against the code before you pass it on.
- Read `claude.permission_denials`. A denied command means a check didn't run.
- `observed.read_only_violation: true` or a non-empty `observed.commits` breaks the contract, so report it. The read-only check only sees changes git can see, and it also catches edits the user makes while Claude runs.
