# Codex Claude Crew

A Codex skill that hands bounded tasks to a local Claude Code session and gets the result back for Codex to check.

Codex writes the brief, Claude reviews code or implements changes in a separate git worktree, and Codex verifies the result.

## Install

Requires Codex, authenticated Claude Code, Python and git on macOS or Linux. See [requirements and supported versions](docs/VERSIONS.md#requirements).

```bash
npx skills add aroramrinaal/codex-claude-crew --skill codex-claude-crew --agent codex -g
```

Omit `-g` to install into the current project only. Then start a new Codex thread and type `$codex-claude-crew`.

## Use

- "Use $codex-claude-crew to have Claude review my uncommitted changes."
- "Ask Claude (opus, high effort) why `test_login` is flaky. Don't change anything."
- "Have Claude implement the retry fix in a worktree, then check its work."

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
