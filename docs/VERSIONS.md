# Version information

## v0.1

- **Review mode** requests read-only work, restricts tools, and checks Git-visible files, the index and HEAD for unexpected changes.
- **Implement mode** has Claude edit files in its own git worktree (branch `crew/<name>`). Codex reads the diff and reruns the checks.
- Each run keeps its Claude session ID, so Codex can send follow-ups into the same session.
- Each run record keeps what Claude claimed and what Codex verified in separate fields.

The long-term goal is one Codex thread coordinating several Claude Code sessions, each in its own worktree.

## Unreleased

- **Pi and OpenCode workers.** `--worker pi` and `--worker opencode` run the same review and implement modes, with the same run record, worktrees, resume, fork, background runs, cancellation and verification. Limits a worker cannot enforce are refused rather than recorded.

- **Renamed to `codex-crew`.** Invoke it as `$codex-crew`. The state folder (`~/.codex-claude-crew`), the `CLAUDE_CREW_*` environment variables and the run record schema keep their old names, so existing runs and worktrees still work.

## Requirements

- Codex (desktop app, CLI or IDE extension)
- At least one authenticated worker CLI. The relay checks the required flags and refuses unsupported versions.
  - [Claude Code](https://code.claude.com/docs/en/getting-started) 2.1.259 or later
  - [Pi](https://github.com/earendil-works/pi) 1.2.0 or later (`npm install -g @earendil-works/pi-coding-agent`)
  - [OpenCode](https://github.com/anomalyco/opencode) 1.19.0 or later (`npm install -g opencode-ai`)
- Python 3.9+, git and `ps`, on macOS or Linux
