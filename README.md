# Codex Claude Crew

A Codex skill that hands bounded tasks to a local Claude Code session and gets the result back for Codex to check.

Codex writes a brief. `relay.py` runs Claude Code headless (`claude -p` with JSON output). Codex verifies what Claude did before it reports back. Claude Code keeps its own context and tool loop, so this is one coding agent directing another.

## What v0.1 does

- **Review mode** is read-only. Claude investigates or reviews code and returns structured findings.
- **Implement mode** has Claude edit files in its own git worktree (branch `crew/<name>`). Codex reads the diff and reruns the checks.
- Each run keeps its Claude session ID, so Codex can send follow-ups into the same session.
- Each run record keeps what Claude claimed and what Codex verified in separate fields.

The long-term goal is one Codex thread coordinating several Claude Code sessions, each in its own worktree.

## Requirements

- Codex (desktop app, CLI or IDE extension)
- [Claude Code](https://code.claude.com/docs/en/getting-started), signed in
- Python 3.9+ and git, on macOS or Linux

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

Every run also gets no MCP servers, denied `git commit` and `git push`, a turn limit, a timeout and a structured report schema. The relay never uses `--dangerously-skip-permissions`.

The relay stores runs in `~/.codex-claude-crew/runs/<run_id>/`: the brief, Claude's raw output and `run.json`. Worktrees go in `~/.codex-claude-crew/worktrees/`. Claude Code keeps its own transcript in `~/.claude/projects/`. To open a run's session yourself, run `claude --resume <session_id>` from the run's folder.

v0.1 calls the `claude` CLI directly rather than the [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview). That keeps the relay to Python's standard library and runs Claude Code with your existing login and settings.

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
python3 -m unittest discover -s tests
```

## Credits

- [GChavez0210/Claude-Delegate](https://github.com/GChavez0210/Claude-Delegate): scoping, the permission boundary, the structured report, verification
- [pejmanjohn/cc-plugin-codex](https://github.com/pejmanjohn/cc-plugin-codex): job lifecycle, background runs, Codex thread tracking
- [amElnagdy/delegate-skills](https://github.com/amElnagdy/delegate-skills): brief writing, the relay pattern, the read-only git tripwire

## License

[MIT](LICENSE)
