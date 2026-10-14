---
name: codex-crew-setup
description: Check which coding-agent workers (Claude Code, Pi, OpenCode) this machine can run for codex-crew, then help the user install or sign in to the ones they want. Use when the user sets up codex-crew, asks which workers or agents are available, or a codex-crew preflight fails.
license: MIT
compatibility: Requires the codex-crew skill installed alongside this one, Python 3.9+, git and ps on macOS or Linux.
metadata:
  version: "0.1.0"
---

# Codex Crew Setup

Find out which workers codex-crew can hand tasks to on this machine, and get the missing ones working. This skill only checks and advises. The user installs software and signs in.

## 1. Find the relay

The relay lives in the codex-crew skill, normally next to this skill's folder: `<skills-dir>/codex-crew/scripts/relay.py`. If it is not there, look in the other skill folders Codex loads. If codex-crew is not installed, tell the user to install it and stop:

```bash
npx skills add MungoHarvey/codex-claude-crew --skill codex-crew --agent codex -g
```

## 2. Check the workers

```bash
python3 <relay> workers
```

This makes no model call and uses no credits. If the user names a Pi model, add `--pi-model <provider/model>` so Pi can check that provider's sign-in.

It prints JSON:

- `host.ok`: git, the state folder and process inspection all work. If false, read `host.notes`; no worker can run until this is fixed.
- `workers[]`: one entry each for `claude`, `pi` and `opencode`.
  - `found`, `binary`, `version`: whether the CLI is on PATH and which version.
  - `capabilities.restricted_profile`: the version is new enough and has every flag the relay needs.
  - `sign_in`: `confirmed`, `not_confirmed`, or `unchecked`. OpenCode, and Pi without `--pi-model`, cannot report sign-in without a model call, so they read `unchecked`.
  - `ready`: codex-crew can run it. `unchecked` sign-in still counts as ready; the first run reports missing credentials.
  - `install` and `sign_in_hint`: what to tell the user.
- `available`: the ready workers. `default`: the one codex-crew will use when the user names none.

## 3. Report

Show one row per worker: found and version, sign-in, ready. Then say which workers are available and which is the default. Keep `install` and sign-in advice to the workers the user wants.

## 4. Fix what the user asks for

- **Missing or too old:** give the `install` command, or the update command for an old version. Run an install only when the user asks you to; global installs change their machine.
- **Not signed in:** give the `sign_in_hint`. Sign-in is interactive and stores credentials, so the user does it in their own terminal.
- **A CLI outside PATH:** point codex-crew at it with `CLAUDE_CREW_BIN`, `CREW_PI_BIN` or `CREW_OPENCODE_BIN`.

After each fix, run `workers` again and report the change.

## 5. Optional live check

Only when the user asks, and after saying it uses their model credits, run one small review per worker they want to confirm:

```bash
python3 <relay> run --worker <worker> --mode review --timeout 120 --brief - <<'EOF'
Goal: Confirm you can read this repository. Read README.md and report one sentence about it.
EOF
```

Add `--model <provider/model>` for Pi and OpenCode if they have no configured default. `status: completed` with a `claimed` report means the worker works end to end. Otherwise report `error`.

## Rules

- Never print, copy or ask for credentials or API keys.
- Don't install software, sign in, or edit a worker's own configuration on the user's behalf unless they ask.
- Don't treat `unchecked` sign-in as confirmed when you report.
