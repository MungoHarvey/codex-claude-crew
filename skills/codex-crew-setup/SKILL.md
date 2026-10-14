---
name: codex-crew-setup
description: Check whether Pi or OpenCode is ready for codex-crew, prefer Pi when the user has no preference, and help install the user's choice if needed. Use when setting up codex-crew or when its worker preflight fails.
license: MIT
compatibility: Requires the codex-crew skill installed alongside this one, Python 3.9+, git and ps on macOS or Linux.
metadata:
  version: "0.1.3"
---

# Codex Crew Setup

Check whether Pi or OpenCode is available for codex-crew, prefer Pi when the user has no preference, and offer to install the chosen worker if it is missing. Installing a global CLI changes the user's machine: ask first and wait for their choice/confirmation before running an install command. Do not configure provider-specific endpoints or credentials as part of this setup.

## 1. Find the relay

The relay lives in the codex-crew skill, normally next to this skill's folder: `<skills-dir>/codex-crew/scripts/relay.py`. If it is not there, look in the other skill folders Codex loads. If codex-crew is not installed, tell the user to install it and stop:

```bash
npx skills add MungoHarvey/codex-claude-crew --skill codex-crew --agent codex -g
```

## 2. Check the host runtime, then Pi and OpenCode

The relay currently supports macOS and Linux only. Before invoking it, identify the current host. On macOS/Linux, verify Python 3.9+, `git` and `ps` are available. On Windows, do not assume that `python`/`python3` is a working interpreter just because `Get-Command` finds a WindowsApps alias: try `py -3 --version`, `python --version` and `python3 --version`, and report the actual launcher output/errors without printing environment secrets. Distinguish an absent interpreter (no usable launcher is installed) from an installed launcher that fails to start.

If Python is genuinely absent, tell the user what was checked and offer to help install Python; wait for explicit confirmation before installing it or changing the machine. If a launcher is installed but fails to start, report the exact error and do not treat that as proof Python is absent or reinstall it automatically. On Windows, also explain that the relay does not currently support native Windows, so installing Windows Python alone will not make the relay work. Do not enable/configure WSL or try another environment as a workaround. Do not proceed to install a worker CLI until the relay host is supported and working. If the user needs Windows support, suggest raising an issue in the project repository and include the exact OS/runtime error they saw.

```bash
python3 <relay> workers
```

Run this only after the host runtime is usable. It makes no model call and uses no credits. It checks all supported worker CLIs; focus the setup decision on Pi and OpenCode. If the user names a Pi model, add `--pi-model <provider/model>` so Pi can check that provider's sign-in. Check `host.ok` before proceeding: if it is false, explain the host prerequisite and stop rather than installing a CLI that this relay cannot run. If the command errors before returning JSON, diagnose whether Python is missing, a launcher failed, or the host is unsupported; report the exact error and ask the user how they want to proceed. Don't install a worker to work around a host/runtime failure.

It prints JSON:

- `host.ok`: git, the state folder and process inspection all work. If false, read `host.notes`; no worker can run until this is fixed.
- `workers[]`: one entry each for `claude`, `pi` and `opencode`.
  - `found`, `binary`, `version`: whether the CLI is on PATH and which version.
  - `capabilities.restricted_profile`: the version is new enough and has every flag the relay needs.
  - `sign_in`: `confirmed`, `not_confirmed`, or `unchecked`. OpenCode, and Pi without `--pi-model`, cannot report sign-in without a model call, so they read `unchecked`.
  - `ready`: codex-crew can run it. `unchecked` sign-in still counts as ready; the first run reports missing credentials.
  - `install` and `sign_in_hint`: what to tell the user.
- `available`: the ready workers. Codex Crew's runtime default is Claude when available, but this setup skill prefers Pi when the user has not named a worker. If Pi is not ready and OpenCode is ready, offer/use OpenCode rather than silently choosing another worker.

## 3. Choose a worker and ask before installing

- If the user named Pi or OpenCode, use that choice. Otherwise prefer ready Pi.
- If Pi is missing or not ready but OpenCode is ready, ask whether the user wants to install/update Pi (the preferred option) or use OpenCode now; don't choose OpenCode for them.
- If the chosen worker is missing or too old, ask whether the user wants it installed/updated. When both Pi and OpenCode are missing and the user has no preference, recommend Pi and ask whether they want Pi or OpenCode. Do not run a global install until the user confirms which one.
- Check that `npm` is available before offering an npm install. If it is missing, report that prerequisite; do not install Node.js as a side effect.
- Install only the selected worker, using its documented package:
  - Pi: `npm install --global @earendil-works/pi-coding-agent`
  - OpenCode: `npm install --global opencode-ai`
- After installation/update, repeat `workers` and verify the chosen worker's version and `capabilities.restricted_profile`. Report the actual result; if it remains unavailable or incompatible, stop and give the reported error rather than guessing at a fix.

## 4. Sign in and report

- If Pi's sign-in is not confirmed, tell the user to start `pi` and use its `/login` flow, or follow the chosen provider's own credential setup. If OpenCode needs authentication, use the relay's `sign_in_hint` (currently `opencode providers login`). Sign-in is interactive and stores credentials; do not run it on the user's behalf unless they explicitly ask.
- Never print, copy, request, or place credentials or API keys into config files. Do not test a model or make a paid/network model call unless the user explicitly asks; explain that a live smoke test may use their provider quota first.
- Show a concise status for Pi and OpenCode (found/version, profile compatibility, sign-in state, ready), identify the selected worker, and distinguish confirmed facts from unchecked authentication.

## Rules

- Do not edit a worker's provider/model configuration as part of setup unless the user asks for that configuration specifically.
- If a CLI is installed outside PATH, explain how to set `CREW_PI_BIN` or `CREW_OPENCODE_BIN`; don't modify shell profiles without permission.
- Don't treat `unchecked` sign-in as confirmed when you report.
