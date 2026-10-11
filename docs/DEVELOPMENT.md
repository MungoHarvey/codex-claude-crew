# Development

## Local installation

From the repository root, link the skill folder so edits are live:

```bash
ln -s "$PWD/skills/codex-claude-crew" ~/.agents/skills/codex-claude-crew
```

Then start a new Codex thread and type `$codex-claude-crew`.

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
