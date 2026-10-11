# Version information

## v0.1

- **Review mode** requests read-only work, restricts tools, and checks Git-visible files, the index and HEAD for unexpected changes.
- **Implement mode** has Claude edit files in its own git worktree (branch `crew/<name>`). Codex reads the diff and reruns the checks.
- Each run keeps its Claude session ID, so Codex can send follow-ups into the same session.
- Each run record keeps what Claude claimed and what Codex verified in separate fields.

The long-term goal is one Codex thread coordinating several Claude Code sessions, each in its own worktree.

## Requirements

- Codex (desktop app, CLI or IDE extension)
- [Claude Code](https://code.claude.com/docs/en/getting-started) 2.1.259 or later, authenticated. The relay checks the required flags and refuses unsupported versions.
- Python 3.9+, git and `ps`, on macOS or Linux
