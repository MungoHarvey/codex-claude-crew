# Development

## Local installation

From the repository root, link the skill folder so edits are live:

```bash
ln -s "$PWD/skills/codex-crew" ~/.agents/skills/codex-crew
```

Then start a new Codex thread and type `$codex-crew`.

## Layout

```text
skills/codex-crew/
  SKILL.md                      when to delegate, how to scope, how to verify
  agents/openai.yaml            Codex display metadata
  scripts/relay.py              runs the worker (Claude, Pi or OpenCode) and writes the run record
  references/task-contract.md   brief template, report and record fields
  scripts/pi_command_gate.js    Pi extension that enforces the run's command rules
skills/codex-crew-setup/
  SKILL.md                      checks which workers are usable (`relay.py workers`)
tests/test_runner.py            relay tests against a fake claude (no model calls)
tests/test_workers.py           relay tests against fake pi and opencode CLIs
```

## Tests

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests
```

The normal suite uses fake workers (`tests/fixtures/fake_claude.py`, `fake_pi.py`, `fake_opencode.py`) and disposable repositories; it makes no model calls. CI runs it on macOS and Linux. To explicitly run the additional live CLI smoke tests with your own Claude credentials and usage, set `CLAUDE_CREW_LIVE_TESTS=1` for the same test command. The live tests require the supported CLI profile and use a temporary repository.
