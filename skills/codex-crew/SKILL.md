---
name: codex-crew
description: Hand a bounded review or implementation task to a local coding-agent worker running headless (Claude Code by default, or Pi or OpenCode), then verify the result yourself. Use when the user asks to delegate work to Claude, Claude Code, Pi or OpenCode, wants a worker's review or a second opinion, or wants a worker to implement a change in a separate git worktree. Not for tasks the user wants you to do directly.
license: MIT
compatibility: Requires at least one authenticated worker CLI (Claude Code 2.1.259+, Pi 1.2.0+ or OpenCode 1.19.0+), Python 3.9+, git and ps on macOS or Linux. Network and credential access follow the host's permission policy.
metadata:
  version: "0.1.0"
---

# Codex Crew

You stay in charge. The worker (Claude Code, Pi or OpenCode) has its own context and tool loop. You scope the task, the worker does it, and you check the result. The worker's report is a claim. Only your own checks confirm it.

Every worker call goes through `scripts/relay.py` in this skill's folder (`<skill-dir>` below). Don't build `claude`, `pi` or `opencode` commands yourself. Below, "Claude" means whichever worker runs; the steps are the same for all three.

## Workers

| `--worker` | CLI | Shell commands | Limits it can enforce |
|---|---|---|---|
| `claude` (default) | Claude Code `claude -p` | Read-only git, plus scoped `--allow` rules | turns, budget, timeout |
| `opencode` | `opencode run` | Read-only git, plus scoped `--allow` rules | turns, timeout |
| `pi` | `pi --mode json` | Read-only git, plus scoped `--allow` rules, enforced by the relay's command gate | timeout only |

Use the worker the user names; otherwise use `claude`. Pick `opencode` or `pi` when the user wants another model provider or a second opinion from a different agent. Pi has no permission system of its own, so the relay loads a small extension that blocks any shell command outside the run's rules; it also blocks chained commands with a part outside the rules, command substitution and redirects that write files. The relay refuses limits a worker cannot enforce (`--max-budget-usd` on Pi and OpenCode, `--max-turns` on Pi) rather than recording them as if they held.

Models: Claude takes aliases (`sonnet`). Pi and OpenCode take `provider/model`, for example `anthropic/claude-sonnet-5-5`; list them with `pi --list-models` or `opencode models`. `--effort` maps to Pi's `--thinking` and OpenCode's `--variant`.

## Setup, once per session

Run `python3 <skill-dir>/scripts/relay.py preflight`, adding `--worker pi` or `--worker opencode` for those workers (Pi also takes `--model` to check that provider's sign-in). It checks CLI capabilities, authentication, process inspection and state-directory access without calling a model. If `ok` is false, resolve the reported prerequisites before delegating. Do not fall back to a weaker permission profile. OpenCode, and Pi without `--model`, cannot confirm sign-in here; a run reports the failure if credentials are missing.

Claude needs network and credential access. Follow the host's configured permission policy; request additional access only when required and supported. Local record commands do not need Claude authentication or network access.

## Modes

| Mode | Use it for | Claude can | Where |
|---|---|---|---|
| `review` | Reviews, bug hunts, questions about the code | Read files, run read-only git commands | `--cwd` (default: current folder). No edits. |
| `implement` | A focused change with clear acceptance criteria | Also edit files and run the commands you `--allow` | A new git worktree on branch `crew/<name>` |

Start with `review` unless the user wants Claude to write code. Delegate substantial, self-contained tasks. A small fix is faster to do yourself.

## 1. Write the brief

Claude sees none of this conversation. Follow [references/task-contract.md](references/task-contract.md): goal, context, scope, acceptance criteria, checks, limits. Restricted mode does not automatically load `CLAUDE.md` or ordinary Claude settings. Include applicable `CLAUDE.md` and `AGENTS.md` rules in the brief, or point to instruction files inside the working directory for Claude to read.

## 2. Pick a model and effort

The user's choice wins. Otherwise:

| Task | `--model` | `--effort` |
|---|---|---|
| Lookups, summaries, mechanical edits | `haiku` | leave out |
| Most reviews and implementation | `sonnet` | `medium` or `high` |
| Hard debugging, security, architecture | `opus` | `high` or `xhigh` |

This table is for `claude`. For Pi and OpenCode, use the model the user asks for, or leave `--model` out to use the worker's configured default. Leave both out to use the defaults available under the restricted profile; user settings are not loaded. Use `fable` only when the user explicitly selects it with awareness that headless requests may bill usage credits. Don't raise the model or effort because a command was denied or a test failed. Fix the brief or the `--allow` rules instead.

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

```bash
python3 <skill-dir>/scripts/relay.py run --worker opencode --mode implement --cwd <repo> --worktree <short-name> \
  --model anthropic/claude-sonnet-5-5 --allow "Bash(npm test *)" --brief - <<'EOF'
<brief>
EOF
```

- `--allow RULE` (repeatable) lets Claude run a command without asking, for example `"Bash(pytest *)"`. Add one for every authorized check. Broad forms such as `Bash` and `Bash(*)` are refused. Shell permissions are not an OS sandbox; choose commands consistent with the mode and owned scope. OpenCode receives the same rules as bash patterns, and Pi's command gate enforces them.
- `--max-turns` (default 50), `--max-budget-usd` and `--timeout` (seconds, default 1800) limit each invocation. A budget is a client-side estimate, not a strict billing ceiling or an aggregate task budget.
- `--base REF` starts the worktree from another commit (default `HEAD`). The user's uncommitted changes are not in the worktree.

Runs take minutes. Give the shell command time for `--timeout` plus startup and cleanup, or add `--background` and poll with `relay.py status <run_id> --wait 30`. `relay.py cancel <run_id>` stops the recorded process group. An `orphaned` run could not be confirmed stopped: inspect its process identities before proceeding; do not verify or resume it.

## 4. Read the run record

The relay prints a JSON record and saves it as `~/.codex-claude-crew/runs/<run_id>/run.json`. The task contract lists every field. The important ones:

- `agent`: the worker that ran (`claude`, `pi` or `opencode`).
- `status`: `completed` means the worker exited successfully and supplied a valid report. `error`, `timed_out`, `interrupted`, `claude_unavailable`, `worker_unavailable` and `worker_incompatible` are failures; `queued`, `running`, `stopping` and `orphaned` retain execution ownership. It doesn't say whether the task is done.
- `claimed`: Claude's report: `status`, `summary`, `findings`, `changed_files`, `checks`, `open_issues`.
- `observed`: what the relay saw in git: `changed_files`, `commits`, `read_only_violation`.
- `claude` holds the worker session for every worker (the name is kept for compatibility). `claude.session_id` is the worker's session id: a UUID for Claude and Pi, `ses_...` for OpenCode. `claude.permission_denials` lists commands the worker tried and wasn't allowed to run.

## 5. Verify, then record it

- Review: check each finding against the code. Drop what you can't confirm.
- Implement: read the full diff (`git -C <worktree> diff <base>` plus untracked files), compare `observed.changed_files` with `claimed.changed_files`, and rerun the acceptance checks in the worktree yourself.
- Tell the user if `observed.read_only_violation` is true or `observed.commits` isn't empty. A null tripwire result means coverage is incomplete; inspect independently. Ignored paths and changes outside the repo are not covered.

```bash
python3 <skill-dir>/scripts/relay.py verify <run_id> --verdict confirmed --check "passed=npm test" --note "<one line>"
```

Verdicts are `confirmed`, `partial` and `rejected`. The relay stores them apart from Claude's claim.

## Follow-ups

`relay.py run --resume <run_id> --brief -` continues the same worker, session, folder and mode. Model, effort, limits and command rules are inherited; explicit options override them. New `--allow` rules extend inherited rules; `--clear-allow` removes them first. Use follow-ups for rework and send the changed requirements. Add `--fork` to branch the conversation after the owner stops; it reuses the checkout, so it is not an independent parallel worker. Active sessions and implementation worktrees cannot be shared by relay runs.

`relay.py list` shows recent runs with their worker and session ids. From the run's folder, the user can open a session with `claude --resume <session_id>`, `pi --session <session_id>` or `opencode --session <session_id>`.

## Report to the user

Say which worker, mode and model ran, what the worker claimed, what you verified and how, and what's still open. Keep "Claude says" apart from "I confirmed". For implement runs, give the worktree path and branch.

## Rules

- Don't commit, push, merge or remove the worktree unless the user asks.
- Never broaden permissions to get past a denial. If Claude needs a command, rerun with a specific `--allow` rule, and only for commands the task needs.
- One task per run.
