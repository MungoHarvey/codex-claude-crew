# How it works

Codex writes a bounded brief. `relay.py` runs Claude Code headless (`claude -p` with JSON output). Claude Code keeps its own context and tool loop; Codex checks its findings or changes before reporting back.

## Run setup

| | review | implement |
|---|---|---|
| Permission mode | `dontAsk` | `acceptEdits` |
| Tools | Read, Glob, Grep, Bash | Read, Glob, Grep, Edit, Write, Bash |
| Commands | Read-only git, plus `--allow` rules | Also file commands like `mkdir` and `mv` |
| Folder | The repo | A new worktree |

Every run uses Claude's restricted profile, disables MCP, Chrome and skills, and sets commit/push deny rules, a turn limit, a timeout and a structured report schema. User/project/local Claude settings and automatic `CLAUDE.md` loading are excluded; put applicable repository instructions in the brief. Managed policy still applies. These are tool permissions, not an OS sandbox: authorized shell commands can have side effects, and the Git tripwire cannot see ignored files or changes outside the repository.

Active sessions and implementation worktrees have exclusive relay ownership. Resume inherits model, effort, command rules and limits; explicit options override them, and `--clear-allow` removes inherited command rules. Budget limits apply per invocation, not to the whole task. A session fork reuses the checkout and can run only after its current owner stops. It is not a parallel worktree fork.

The relay stores runs in `~/.codex-claude-crew/runs/<run_id>/`: the brief, Claude's raw output and `run.json`. Worktrees go in `~/.codex-claude-crew/worktrees/`. Claude Code keeps its own transcript in `~/.claude/projects/`. To open a run's session yourself, run `claude --resume <session_id>` from the run's folder.

The relay calls the `claude` CLI directly rather than the [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview). That keeps the runtime to Python's standard library and uses your Claude authentication with an explicit execution profile. Follow the host's permission policy when granting network or credential access.

Cancellation stops the recorded process group, including children that outlive its leader. Status reconciles crashed workers. If it cannot establish process ownership or confirm a stop, the run stays `orphaned`, retains its reservation, and cannot be verified or resumed. Inspect the recorded process group before proceeding; commands that detach into other groups are outside this mechanism.
