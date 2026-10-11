---
name: codex-claude-crew
description: Hand a bounded review or implementation task to a local Claude Code session running headless (`claude -p`), then verify the result yourself. Use when the user asks to delegate work to Claude or Claude Code, wants Claude's review or a second opinion, or wants Claude to implement a change in a separate git worktree. Not for tasks the user wants you to do directly.
license: MIT
compatibility: Needs the Claude Code CLI (`claude`) installed and signed in, Python 3.9+, and git, on macOS or Linux. Claude runs outside the Codex sandbox, so each run needs approval.
metadata:
  version: "0.1.0"
---

# Codex Claude Crew

You stay in charge. Claude Code is a worker with its own context and tool loop. You scope the task, Claude does it, and you check the result. Claude's report is a claim. Only your own checks confirm it.

Every Claude call goes through `scripts/relay.py` in this skill's folder (`<skill-dir>` below). Don't build `claude` commands yourself.

## Setup, once per session

Run `python3 <skill-dir>/scripts/relay.py preflight`. It checks the CLI, its version and the sign-in without calling a model. If `ok` is false, tell the user what `notes` says and stop.

Run every relay command outside the Codex sandbox (request escalated permissions). Claude needs the network and its stored login. Inside the sandbox it fails or reports that it's signed out.

## Modes

| Mode | Use it for | Claude can | Where |
|---|---|---|---|
| `review` | Reviews, bug hunts, questions about the code | Read files, run read-only git commands | `--cwd` (default: current folder). No edits. |
| `implement` | A focused change with clear acceptance criteria | Also edit files and run the commands you `--allow` | A new git worktree on branch `crew/<name>` |

Start with `review` unless the user wants Claude to write code. Delegate substantial, self-contained tasks. A small fix is faster to do yourself.

## 1. Write the brief

Claude sees none of this conversation. Follow [references/task-contract.md](references/task-contract.md): goal, context, scope, acceptance criteria, checks, limits. Claude reads the repo's `CLAUDE.md` but not `AGENTS.md`, so copy any `AGENTS.md` rules that matter into the brief.

## 2. Pick a model and effort

The user's choice wins. Otherwise:

| Task | `--model` | `--effort` |
|---|---|---|
| Lookups, summaries, mechanical edits | `haiku` | leave out |
| Most reviews and implementation | `sonnet` | `medium` or `high` |
| Hard debugging, security, architecture | `opus` | `high` or `xhigh` |
| The hardest problems, if the account has it | `fable` | `high` |

Leave both out to use the user's own Claude Code defaults. Don't raise the model or effort because a command was denied or a test failed. Fix the brief or the `--allow` rules instead.

## 3. Run

```bash
python3 <skill-dir>/scripts/relay.py run --mode review --cwd <repo> --model sonnet --effort high --brief - <<'EOF'
<brief>
EOF
```

```bash
python3 <skill-dir>/scripts/relay.py run --mode implement --cwd <repo> --worktree <short-name> \
  --allow "Bash(npm test *)" --brief - <<'EOF'
<brief>
EOF
```

- `--allow RULE` (repeatable) lets Claude run a command without asking, for example `"Bash(pytest *)"`. Add one for every check in the brief. Bare `Bash` is refused.
- `--max-turns` (default 50), `--max-budget-usd` and `--timeout` (seconds, default 1800) limit the run.
- `--base REF` starts the worktree from another commit (default `HEAD`). The user's uncommitted changes are not in the worktree.

Runs take minutes. Give the shell command a timeout longer than `--timeout`, or add `--background` and poll with `relay.py status <run_id> --wait 600`. `relay.py cancel <run_id>` stops a run.

## 4. Read the run record

The relay prints a JSON record and saves it as `~/.codex-claude-crew/runs/<run_id>/run.json`. The task contract lists every field. The important ones:

- `status`: whether Claude finished (`completed`) or not (`error`, `timed_out`, `interrupted`, `claude_unavailable`). It doesn't say whether the task is done.
- `claimed`: Claude's report: `status`, `summary`, `findings`, `changed_files`, `checks`, `open_issues`.
- `observed`: what the relay saw in git: `changed_files`, `commits`, `read_only_violation`.
- `claude.session_id` is the Claude Code session. `claude.permission_denials` lists commands Claude tried and wasn't allowed to run.

## 5. Verify, then record it

- Review: check each finding against the code. Drop what you can't confirm.
- Implement: read the full diff (`git -C <worktree> diff <base>` plus untracked files), compare `observed.changed_files` with `claimed.changed_files`, and rerun the acceptance checks in the worktree yourself.
- Tell the user if `observed.read_only_violation` is true or `observed.commits` isn't empty.

```bash
python3 <skill-dir>/scripts/relay.py verify <run_id> --verdict confirmed --check "passed=npm test" --note "<one line>"
```

Verdicts are `confirmed`, `partial` and `rejected`. The relay stores them apart from Claude's claim.

## Follow-ups

`relay.py run --resume <run_id> --brief -` sends a follow-up into the same Claude session, in the same folder and mode, so Claude keeps its context. Use it for rework, and send only what changes. Add `--fork` to branch the session instead.

`relay.py list` shows recent runs with their session ids. The user can open any session in Claude Code with `claude --resume <session_id>` from the run's folder.

## Report to the user

Say which mode and model ran, what Claude claimed, what you verified and how, and what's still open. Keep "Claude says" apart from "I confirmed". For implement runs, give the worktree path and branch.

## Rules

- Don't commit, push, merge or remove the worktree unless the user asks.
- Never broaden permissions to get past a denial. If Claude needs a command, rerun with a specific `--allow` rule, and only for commands the task needs.
- One task per run.
