# Codex Crew

A Codex skill that hands bounded tasks to a local coding-agent worker (Claude Code, [Pi](https://github.com/earendil-works/pi) or [OpenCode](https://github.com/anomalyco/opencode)) and gets the result back for Codex to check.

Codex writes the brief, the worker reviews code or implements changes in a separate git worktree, and Codex verifies the result.

## Install as a Codex plugin

From the repository root, register the local marketplace and install the plugin:

```bash
codex plugin marketplace add .
codex plugin add codex-crew --marketplace codex-crew-local
```

Restart Codex (or start a new thread) and invoke `$codex-crew` or `$codex-crew-setup`.
The plugin packages both skills and their relay scripts.

## Install individual skills

Requires Codex, at least one authenticated worker CLI (Claude Code, Pi or OpenCode), Python and git on macOS or Linux. See [requirements and supported versions](docs/VERSIONS.md#requirements).

Cursor Agent is not currently a supported relay worker. Although its CLI is available on some machines, this project does not yet have a Cursor adapter; the relay also depends on POSIX process and file-locking APIs and does not run natively on Windows.

```bash
npx skills add MungoHarvey/codex-claude-crew --skill codex-crew --agent codex -g
```

Omit `-g` to install into the current project only. Install the setup skill too if you want it to check for Pi and OpenCode, recommend Pi when you have no preference, and ask before installing your choice:

```bash
npx skills add MungoHarvey/codex-claude-crew --skill codex-crew-setup --agent codex -g
```

Then start a new Codex thread and type `$codex-crew-setup` to check Pi and OpenCode and walk through installing your chosen worker if needed, or `$codex-crew` to hand off a task.

## Use

- "Use $codex-crew to have Claude review my uncommitted changes."
- "Ask Claude (opus, high effort) why `test_login` is flaky. Don't change anything."
- "Have Claude implement the retry fix in a worktree, then check its work."
- "Get a second opinion on this diff from OpenCode."
- "Ask Pi to review `src/auth/` for token-handling bugs."

## Documentation

- [How it works](docs/HOW_IT_WORKS.md)
- [Version information](docs/VERSIONS.md)
- [Development](docs/DEVELOPMENT.md)

## Credits

- [GChavez0210/Claude-Delegate](https://github.com/GChavez0210/Claude-Delegate): scoping, the permission boundary, the structured report, verification
- [pejmanjohn/cc-plugin-codex](https://github.com/pejmanjohn/cc-plugin-codex): job lifecycle, background runs, Codex thread tracking
- [amElnagdy/delegate-skills](https://github.com/amElnagdy/delegate-skills): brief writing, the relay pattern, the read-only git tripwire

## License

[MIT](LICENSE)
