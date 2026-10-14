# Skill evals

Behavioural evals for the `codex-crew` and `codex-crew-setup` skills. Each case runs twice: once with the skill available and once without it (the baseline). Runs use the fake `claude`, `pi` and `opencode` CLIs from `tests/fixtures/`, so no model is called and no credits are spent. They test whether an agent follows the skill, not how good a worker model is.

| File | What it is |
|---|---|
| `<skill>/evals.json` | Prompts and assertions per case, including a negative case where the skill should stay out of the way |
| `make_env.sh` | Builds one sandbox: fake CLIs first on `PATH`, its own `CLAUDE_CREW_HOME`, and a small git repo with two division-by-zero bugs. Extra arguments name workers to leave out |
| `grade.py` | Grades runs from what happened (relay run records, fake-CLI call logs, repo state), plus keyword checks on the reply. Writes skill-creator `grading.json` files |
| `results/` | `benchmark.json` from the latest run, from skill-creator's `aggregate_benchmark` |

## Running

1. For each case and configuration, run `evals/make_env.sh <workspace>/<crew|setup>-<id>-<name>/<with_skill|without_skill> [missing-worker...]`.
2. Give an agent the case prompt, with the skill path for `with_skill` and none for `without_skill`. Tell it to start each shell command with `. <run>/env.sh &&`, write its reply to `<run>/outputs/answer.md` and list its commands in `<run>/transcript.md`. Save `timing.json` from the agent's token and duration totals.
3. Grade and aggregate:

```bash
python3 evals/grade.py <workspace> <bench>
python3 -m scripts.aggregate_benchmark <bench>/codex-crew --skill-name codex-crew   # from skill-creator
```

## Latest results (14/10/2026)

| Skill | With skill | Without | Cases |
|---|---|---|---|
| codex-crew | 100% | 53% | 4 (one negative) |
| codex-crew-setup | 100% | 48% | 2 |

One run per configuration, so there is no variance estimate. The negative case (a one-line typo fix) passes both ways, as intended. Baselines reasoned well, but called `claude` and `pi` directly, outside the relay's restricted profile, Pi command gate, run record and worktree; most of the gap is that. The Pi budget assertion was widened after the first run: stopping to offer alternatives before spending is as correct as running Pi bounded by a timeout.
