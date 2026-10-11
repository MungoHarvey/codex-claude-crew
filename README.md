# Codex Claude Crew

A Codex skill that hands bounded tasks to a local Claude Code session and gets the result back for Codex to check.

Codex writes a brief. `relay.py` runs Claude Code headless (`claude -p` with JSON output). Codex verifies what Claude did before it reports back. Claude Code keeps its own context and tool loop, so this is one coding agent directing another.

## What v0.1 does

- **Review mode** requests read-only work, restricts tools, and checks Git-visible files, the index and HEAD for unexpected changes.
- **Implement mode** has Claude edit files in its own git worktree (branch `crew/<name>`). Codex reads the diff and reruns the checks.
- Each run keeps its Claude session ID, so Codex can send follow-ups into the same session.
- Each run record keeps what Claude claimed and what Codex verified in separate fields.

The long-term goal is one Codex thread coordinating several Claude Code sessions, each in its own worktree.

## Requirements

- Codex (desktop app, CLI or IDE extension)
- [Claude Code](https://code.claude.com/docs/en/getting-started) 2.1.259 or later, authenticated. The relay checks the required flags and refuses unsupported versions.
- Python 3.9+, git and `ps`, on macOS or Linux

## Install

```bash
npx skills add aroramrinaal/codex-claude-crew --skill codex-claude-crew --agent codex -g
```

Without `-g`, the skill installs into the current project only. For development, link the skill folder instead so edits are live:

```bash
ln -s "$PWD/skills/codex-claude-crew" ~/.agents/skills/codex-claude-crew
```

Then start a new Codex thread and type `$codex-claude-crew`.

## Use

- "Use $codex-claude-crew to have Claude review my uncommitted changes."
- "Ask Claude (opus, high effort) why `test_login` is flaky. Don't change anything."
- "Have Claude implement the retry fix in a worktree, then check its work."

## How a run is set up

| | review | implement |
|---|---|---|
| Permission mode | `dontAsk` | `acceptEdits` |
| Tools | Read, Glob, Grep, Bash | Read, Glob, Grep, Edit, Write, Bash |
| Commands | Read-only git, plus `--allow` rules | Also file commands like `mkdir` and `mv` |
| Folder | The repo | A new worktree |

Every run uses Claude's restricted profile, disables MCP, Chrome and skills, and sets commit/push deny rules, a turn limit, a timeout and a structured report schema. User/project/local Claude settings and automatic `CLAUDE.md` loading are excluded; put applicable repository instructions in the brief. Managed policy still applies. These are tool permissions, not an OS sandbox: authorized shell commands can have side effects, and the Git tripwire cannot see ignored files or changes outside the repository.

Active sessions and implementation worktrees have exclusive relay ownership. Resume inherits model, effort, command rules and limits; explicit options override them, and `--clear-allow` removes inherited command rules. Budget limits apply per invocation, not to the whole task. A session fork reuses the checkout and can run only after its current owner stops. It is not a parallel worktree fork.

The relay stores runs in `~/.codex-claude-crew/runs/<run_id>/`: the brief, Claude's raw output and `run.json`. Worktrees go in `~/.codex-claude-crew/worktrees/`. Claude Code keeps its own transcript in `~/.claude/projects/`. To open a run's session yourself, run `claude --resume <session_id>` from the run's folder.

v0.1 calls the `claude` CLI directly rather than the [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview). That keeps the runtime to Python's standard library and uses your Claude authentication with an explicit execution profile. Follow the host's permission policy when granting network or credential access.

Cancellation stops the recorded process group, including children that outlive its leader. Status reconciles crashed workers. If it cannot establish process ownership or confirm a stop, the run stays `orphaned`, retains its reservation, and cannot be verified or resumed. Inspect the recorded process group before proceeding; commands that detach into other groups are outside this mechanism.

## Layout

```text
skills/codex-claude-crew/
  SKILL.md                      when to delegate, how to scope, how to verify
  agents/openai.yaml            Codex display metadata
  scripts/relay.py              runs Claude and writes the run record
  references/task-contract.md   brief template, report and record fields
tests/test_runner.py            relay tests against a fake claude (no model calls)
```

## Tests

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests
```

The normal suite uses fake workers and disposable repositories; it makes no model calls. CI runs it on macOS and Linux. To explicitly run the additional live CLI smoke tests with your own Claude credentials and usage, set `CLAUDE_CREW_LIVE_TESTS=1` for the same test command. The live tests require the supported CLI profile and use a temporary repository.

## Credits

- [GChavez0210/Claude-Delegate](https://github.com/GChavez0210/Claude-Delegate): scoping, the permission boundary, the structured report, verification
- [pejmanjohn/cc-plugin-codex](https://github.com/pejmanjohn/cc-plugin-codex): job lifecycle, background runs, Codex thread tracking
- [amElnagdy/delegate-skills](https://github.com/amElnagdy/delegate-skills): brief writing, the relay pattern, the read-only git tripwire

## License

[MIT](LICENSE)
