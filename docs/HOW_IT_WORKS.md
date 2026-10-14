# How it works

Codex writes a bounded brief. `relay.py` runs a worker headless: Claude Code (`claude -p` with JSON output, the default), Pi (`pi --mode json`) or OpenCode (`opencode run --format json`). The worker keeps its own context and tool loop; Codex checks its findings or changes before reporting back.

## Run setup

| | review | implement |
|---|---|---|
| Permission mode | `dontAsk` | `acceptEdits` |
| Tools | Read, Glob, Grep, Bash | Read, Glob, Grep, Edit, Write, Bash |
| Commands | Read-only git, plus `--allow` rules | Also file commands like `mkdir` and `mv` |
| Folder | The repo | A new worktree |

Every Claude run uses Claude's restricted profile, disables MCP, Chrome and skills, and sets commit/push deny rules, a turn limit, a timeout and a structured report schema. User/project/local Claude settings and automatic `CLAUDE.md` loading are excluded; put applicable repository instructions in the brief. Managed policy still applies. These are tool permissions, not an OS sandbox: authorized shell commands can have side effects, and the Git tripwire cannot see ignored files or changes outside the repository.

## Pi and OpenCode workers

| | Pi | OpenCode |
|---|---|---|
| Permission boundary | Tool allowlist (`--tools`). No per-command rules | A relay-defined agent (`codex-crew-review` or `codex-crew-implement`) in `OPENCODE_CONFIG_CONTENT`, with `"*": "deny"` first and specific allows after |
| review tools | `read`, `grep`, `find`, `ls`; no shell | read, glob, grep, list; bash denied except read-only git and `--allow` patterns |
| implement tools | Adds `edit`, `write`. `bash` only with `--unscoped-bash`, and then unrestricted | Adds `edit`; same bash rules |
| Isolation | `--no-extensions --no-skills --no-prompt-templates --no-themes --no-context-files --no-mcp --no-approve --offline` | `OPENCODE_DISABLE_PROJECT_CONFIG`, `OPENCODE_PURE`, `OPENCODE_DISABLE_CLAUDE_CODE`, `OPENCODE_DISABLE_EXTERNAL_SKILLS`, `OPENCODE_DISABLE_AUTOUPDATE`, `OPENCODE_DISABLE_SHARE` |
| Worker rules and report schema | `--append-system-prompt` | Prepended to the brief on stdin |
| Limits | Timeout only | `steps` (from `--max-turns`) and timeout |
| Sessions | `--session-id` up front; `--session` to resume; `--fork` | Id read from the event stream; `--session` to resume; `--fork` |

OpenCode evaluates permissions last-match-wins, and an agent's rules are appended after the user's global rules, so the relay's catch-all deny overrides anything broader in the user's own config. `opencode run` without `--auto` rejects any request that would otherwise prompt, and the relay never passes `--auto`. User-global OpenCode config (providers, models) still loads; project config does not.

Pi has no permission system: its own documentation says to treat model-generated commands as untrusted and to rely on an operating-system or container boundary. The relay therefore never gives a Pi review a shell, and gives an implement run one only on explicit request. Pi's JSON mode exits 0 when a response fails, so the relay reads each assistant message's stop reason rather than trusting the exit code.

Neither CLI takes a JSON schema for its answer, so the worker is told to end with the report in a fenced JSON block, and the relay validates it against the same schema Claude uses. A missing or invalid report fails the run.

## Ownership and cancellation

Active sessions and implementation worktrees have exclusive relay ownership. Resume inherits model, effort, command rules and limits; explicit options override them, and `--clear-allow` removes inherited command rules. Budget limits apply per invocation, not to the whole task. A session fork reuses the checkout and can run only after its current owner stops. It is not a parallel worktree fork.

The relay stores runs in `~/.codex-claude-crew/runs/<run_id>/`: the brief, the worker's raw output and `run.json`. Worktrees go in `~/.codex-claude-crew/worktrees/`. Claude Code keeps its transcript in `~/.claude/projects/`, Pi in `~/.pi/agent/sessions/`, and OpenCode in its own database. To open a run's session yourself, run `claude --resume <session_id>`, `pi --session <session_id>` or `opencode --session <session_id>` from the run's folder.

The relay calls the `claude` CLI directly rather than the [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview). That keeps the runtime to Python's standard library and uses your Claude authentication with an explicit execution profile. Follow the host's permission policy when granting network or credential access.

Cancellation stops the recorded process group, including children that outlive its leader. Status reconciles crashed workers. If it cannot establish process ownership or confirm a stop, the run stays `orphaned`, retains its reservation, and cannot be verified or resumed. Inspect the recorded process group before proceeding; commands that detach into other groups are outside this mechanism.
