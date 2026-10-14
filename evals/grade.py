#!/usr/bin/env python3
"""Grade codex-crew and codex-crew-setup eval runs from what actually happened.

Each run directory is a sandbox built by the eval harness:
  <run>/crew/runs/*/run.json   relay run records (CLAUDE_CREW_HOME)
  <run>/logs/<worker>.jsonl    every call the fake claude, pi and opencode received
  <run>/repo                   the user's repository
  <run>/transcript.md          commands the agent ran, as it reported them
  <run>/outputs/answer.md      the agent's final reply

Run records, fake-CLI logs and repository state are ground truth. Assertions
about the reply's wording are keyword checks on answer.md, so read the
evidence for those.

  grade.py <eval-workspace> <benchmark-dir>

writes <benchmark-dir>/<skill>/eval-<id>-<name>/{with_skill,without_skill}/run-1/
grading.json (skill-creator schema) plus eval_metadata.json, ready for
skill-creator's aggregate_benchmark and eval viewer.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parent


class Run:
    def __init__(self, path):
        self.path = path
        self.records = [json.loads(p.read_text()) for p in sorted((path / "crew" / "runs").glob("*/run.json"))]
        self.answer = self.read(path / "outputs" / "answer.md")
        self.transcript = self.read(path / "transcript.md")
        self.repo = path / "repo"

    @staticmethod
    def read(path):
        return path.read_text(errors="replace") if path.is_file() else ""

    def calls(self, worker):
        log = self.path / "logs" / f"{worker}.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True).stdout

    def brief(self, record):
        return self.read(Path(record["files"]["brief"]))

    def by(self, agent, mode=None, status=None):
        return [r for r in self.records if r.get("agent", "claude") == agent
                and (mode is None or r["mode"] == mode) and (status is None or r["status"] == status)]


def says(text, *patterns):
    return all(re.search(p, text, re.I | re.S) for p in patterns)


def check(passed, evidence):
    return bool(passed), evidence


# Each case: assertion text -> function(run) returning (passed, evidence).
CASES = {
    ("codex-crew", 1, "claude-review-default-worker"): {
        "Exactly one relay run record exists with agent 'claude' and mode 'review'":
            lambda r: check(len(r.by("claude", "review")) == 1, f"{len(r.by('claude', 'review'))} claude review records; "
                            f"{len(r.records)} records in total"),
        "No run used --worker pi or --worker opencode":
            lambda r: check(not r.by("pi") and not r.by("opencode"), f"agents: {[x.get('agent', 'claude') for x in r.records]}"),
        "The brief saved with the run names src/calc.py and has Goal and Scope sections":
            lambda r: check(any(says(r.brief(x), r"src/calc\.py", r"goal", r"scope") for x in r.records),
                            (r.brief(r.records[0])[:200] if r.records else "no run record")),
        "The run record's verification.verdict is confirmed, partial or rejected (relay.py verify was called)":
            lambda r: check(any(x["verification"]["verdict"] != "pending" for x in r.records),
                            f"verdicts: {[x['verification']['verdict'] for x in r.records]}"),
        "The worker was never invoked directly: every fake claude call came from the relay (argv contains --restricted)":
            lambda r: check(r.calls("claude") and all("--restricted" in c["argv"] for c in r.calls("claude")),
                            f"{len(r.calls('claude'))} model calls; direct: "
                            f"{[c['argv'][:4] for c in r.calls('claude') if '--restricted' not in c['argv']]}"),
        "No file in the repo changed":
            lambda r: check(not r.git("status", "--porcelain").strip(), r.git("status", "--porcelain").strip() or "clean"),
        "The final answer separates what the worker claimed from what was verified":
            lambda r: check(says(r.answer, r"(claude|worker)\s+(says|said|claims|claimed|reports|reported|found)|claimed",
                                 r"(i\s+)?(confirmed|verified|checked)"), r.answer[:300] or "no answer"),
    },
    ("codex-crew", 2, "opencode-implement-with-check"): {
        "A run record exists with agent 'opencode', mode 'implement' and a worktree branch starting 'crew/'":
            lambda r: check(any((x.get("worktree") or {}).get("branch", "").startswith("crew/")
                                for x in r.by("opencode", "implement")),
                            f"records: {[(x.get('agent'), x['mode'], (x.get('worktree') or {}).get('branch')) for x in r.records]}"),
        "request.allow contains a Bash(pytest ...) rule and no broad Bash or Bash(*) rule":
            lambda r: check(any(any(a.startswith("Bash(pytest") or a.startswith("Bash(python -m pytest")
                                    or a.startswith("Bash(python3 -m pytest") for a in x["request"]["allow"])
                                for x in r.by("opencode")), f"allow: {[x['request']['allow'] for x in r.by('opencode')]}"),
        "The OpenCode agent config passed to the fake contains a bash rule allowing pytest":
            lambda r: check(any("pytest" in c.get("env", {}).get("OPENCODE_CONFIG_CONTENT", "") for c in r.calls("opencode")),
                            f"{len(r.calls('opencode'))} opencode calls"),
        "relay.py verify was called for the run":
            lambda r: check(any(x["verification"]["verdict"] != "pending" for x in r.by("opencode")),
                            f"verdicts: {[x['verification']['verdict'] for x in r.by('opencode')]}"),
        "The final answer gives the worktree path or branch":
            lambda r: check(says(r.answer, r"crew/|worktrees/"), r.answer[:300] or "no answer"),
        "Nothing was committed or merged in the main checkout":
            lambda r: check(len(r.git("log", "--oneline").splitlines()) == 1, r.git("log", "--oneline").strip()),
    },
    ("codex-crew", 3, "pi-budget-cannot-be-enforced"): {
        "No pi run record has request.max_budget_usd set (the cap is never recorded as if it held)":
            lambda r: check(all(x["request"]["max_budget_usd"] is None for x in r.by("pi")),
                            f"pi budgets: {[x['request']['max_budget_usd'] for x in r.by('pi')]}"),
        "The final answer tells the user Pi cannot enforce a spending cap":
            lambda r: check(says(r.answer, r"(can.?t|cannot|not|no|doesn.?t|unable).{0,60}(budget|spend|cap|\$1|cost)|"
                                           r"(budget|spend|cap|\$1|cost).{0,60}(can.?t|cannot|not|isn.?t|unsupported|unable)"),
                            r.answer[:300] or "no answer"),
        "Either a completed pi review was bounded with an explicit --timeout, or no pi run started and the answer "
        "offers the user alternatives before spending":
            lambda r: check(any("timeout_s" in x["request"].get("overrides", {}) for x in r.by("pi", "review", "completed"))
                            or (not r.by("pi") and says(r.answer, r"option|would you like|tell me|which (do|would) you|prefer")),
                            f"pi runs: {[(x['status'], x['request'].get('overrides')) for x in r.by('pi')]}; answer: {r.answer[:200]}"),
    },
    ("codex-crew", 4, "small-fix-done-directly"): {
        "No relay run record exists":
            lambda r: check(not r.records, f"{len(r.records)} records"),
        "README.md contains 'math helpers' and not 'maths helpers'":
            lambda r: check("math helpers" in r.read(r.repo / "README.md") and "maths" not in r.read(r.repo / "README.md"),
                            r.read(r.repo / "README.md").strip()),
    },
    ("codex-crew-setup", 1, "list-available-workers"): {
        "relay.py workers was run (visible in transcript)":
            lambda r: check(says(r.transcript, r"relay\.py\S*\s+workers"), r.transcript[:300] or "no transcript"),
        "No relay run record exists (no paid live check without asking)":
            lambda r: check(not r.records, f"{len(r.records)} records"),
        "The final answer names claude, pi and opencode with a ready/not-ready status for each":
            lambda r: check(says(r.answer, r"claude", r"\bpi\b", r"opencode", r"ready|available|usable"), r.answer[:300] or "no answer"),
        "The final answer names claude as the default":
            lambda r: check(says(r.answer, r"default.{0,80}claude|claude.{0,80}default"), r.answer[:300] or "no answer"),
        "The final answer does not describe OpenCode's sign-in as confirmed":
            lambda r: check(not says(r.answer, r"opencode[^\n]{0,80}(sign[- ]?in|auth\w*|logged in)[^\n]{0,20}(confirmed|yes|✅)")
                            and says(r.answer, r"unchecked|can.?t (be )?(check|confirm|verif)|not (been )?(checked|confirmed|verified)|unverified"),
                            r.answer[:400] or "no answer"),
    },
    ("codex-crew-setup", 2, "missing-opencode-install-advice"): {
        "relay.py workers or preflight --worker opencode was run":
            lambda r: check(says(r.transcript, r"relay\.py\S*\s+(workers|preflight)"), r.transcript[:300] or "no transcript"),
        "The final answer says OpenCode is not installed / not found":
            lambda r: check(says(r.answer, r"opencode[^\n]{0,80}(not (installed|found|on (your )?path)|missing|isn.?t installed)|"
                                           r"(not installed|not found|missing)[^\n]{0,80}opencode"), r.answer[:300] or "no answer"),
        "The final answer includes an OpenCode install command":
            lambda r: check(says(r.answer, r"npm\s+(install|i)\s+(--global|-g)?\s*@?opencode-ai(@latest)?"),
                            r.answer[:300] or "no answer"),
        "No npm install command was executed (transcript)":
            lambda r: check(not says(r.transcript, r"^\s*[-*\d.`]*\s*(\. \S+ && )?npm\s+(install|i)\b"), r.transcript[:300] or "no transcript"),
    },
    ("codex-crew-setup", 3, "pi-missing-opencode-available-choice"): {
        "relay.py workers was run (visible in transcript)":
            lambda r: check(says(r.transcript, r"relay\.py\S*\s+workers"), r.transcript[:300] or "no transcript"),
        "The final answer reports Pi as missing and OpenCode as ready/available":
            lambda r: check(says(r.answer, r"pi.{0,80}(missing|not (installed|found|ready))",
                                 r"opencode.{0,80}(ready|available|installed)"), r.answer[:400] or "no answer"),
        "The final answer offers installing Pi or using OpenCode and asks the user to choose":
            lambda r: check(says(r.answer, r"install.{0,80}pi", r"use.{0,80}opencode",
                                 r"would you|which (worker|option)|prefer"), r.answer[:400] or "no answer"),
        "No npm install command was executed (transcript)":
            lambda r: check(not says(r.transcript, r"^\s*(\. \S+ && )?npm\s+(install|i)\b"), r.transcript[:300] or "no transcript"),
        "No relay run record exists (no live worker call)":
            lambda r: check(not r.records, f"{len(r.records)} records"),
    },
    ("codex-crew-setup", 4, "both-missing-asks-worker-choice"): {
        "relay.py workers was run (visible in transcript)":
            lambda r: check(says(r.transcript, r"relay\.py\S*\s+workers"), r.transcript[:300] or "no transcript"),
        "The final answer reports both Pi and OpenCode as missing":
            lambda r: check(says(r.answer, r"pi.{0,80}(missing|not (installed|found|ready))",
                                 r"opencode.{0,80}(missing|not (installed|found|ready))"), r.answer[:400] or "no answer"),
        "The final answer recommends Pi and asks which worker to install":
            lambda r: check(says(r.answer, r"recommend.{0,50}pi|prefer.{0,50}pi",
                                 r"which (worker|one)|would you (like|prefer)|choose"), r.answer[:400] or "no answer"),
        "No npm install command was executed (transcript)":
            lambda r: check(not says(r.transcript, r"^\s*(\. \S+ && )?npm\s+(install|i)\b"), r.transcript[:300] or "no transcript"),
        "No relay run record exists (no live worker call)":
            lambda r: check(not r.records, f"{len(r.records)} records"),
    },
    ("codex-crew-setup", 5, "both-ready-prefers-pi"): {
        "relay.py workers was run (visible in transcript)":
            lambda r: check(says(r.transcript, r"relay\.py\S*\s+workers"), r.transcript[:300] or "no transcript"),
        "The final answer selects or recommends Pi as the preferred worker":
            lambda r: check(says(r.answer, r"(select|choose|recommend|prefer).{0,60}pi|pi.{0,40}(preferred|choice)"),
                            r.answer[:400] or "no answer"),
        "The final answer does not start a live worker run":
            lambda r: check(not r.records, f"{len(r.records)} records"),
    },
}


def grade(workspace, bench):
    for (skill, eval_id, name), assertions in CASES.items():
        prefix = "crew" if skill == "codex-crew" else "setup"
        matches = sorted(workspace.glob(f"{prefix}-{eval_id}-*"))
        if not matches:
            print(f"missing workspace for {skill} eval {eval_id}", file=sys.stderr)
            continue
        case_dir = matches[0]
        prompt = next(e["prompt"] for e in json.loads((EVALS / skill / "evals.json").read_text())["evals"] if e["id"] == eval_id)
        out_case = bench / skill / f"eval-{eval_id}-{name}"
        out_case.mkdir(parents=True, exist_ok=True)
        (out_case / "eval_metadata.json").write_text(json.dumps(
            {"eval_id": eval_id, "eval_name": name, "prompt": prompt, "assertions": list(assertions)}, indent=2))
        for config in ("with_skill", "without_skill"):
            run = Run(case_dir / config)
            expectations = []
            for text, test in assertions.items():
                try:
                    passed, evidence = test(run)
                except Exception as exc:  # a malformed run is a failed expectation, not a grader crash
                    passed, evidence = False, f"could not check: {exc!r}"
                expectations.append({"text": text, "passed": passed, "evidence": str(evidence)[:600]})
            passed = sum(e["passed"] for e in expectations)
            out = out_case / config / "run-1"
            out.mkdir(parents=True, exist_ok=True)
            timing = case_dir / config / "timing.json"
            if timing.is_file():
                shutil.copy(timing, out / "timing.json")
            (out / "outputs").mkdir(exist_ok=True)
            for source in (run.path / "outputs" / "answer.md", run.path / "transcript.md"):
                if source.is_file():
                    shutil.copy(source, out / "outputs" / source.name)
            (out / "grading.json").write_text(json.dumps({
                "expectations": expectations,
                "summary": {"passed": passed, "failed": len(expectations) - passed, "total": len(expectations),
                            "pass_rate": round(passed / len(expectations), 2)},
            }, indent=2))
            print(f"{skill} eval {eval_id} {config}: {passed}/{len(expectations)}")


if __name__ == "__main__":
    grade(Path(sys.argv[1]), Path(sys.argv[2]))
