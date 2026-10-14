"""Relay tests for the Pi and OpenCode workers, against fake CLIs (no model calls).

Run with: python3 -m unittest discover -s tests
"""

import json
import sys
from pathlib import Path

from test_runner import ROOT, RelayHarness

FIXTURES = ROOT / "tests" / "fixtures"


class WorkerHarness(RelayHarness):
    def setUp(self):
        super().setUp()
        self.worker_log = self.tmp / "worker-calls.jsonl"
        for name, variable in (("pi", "CREW_PI_BIN"), ("opencode", "CREW_OPENCODE_BIN")):
            shim = self.tmp / name
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURES / f"fake_{name}.py"}" "$@"\n')
            shim.chmod(0o755)
            self.env[variable] = str(shim)
        self.env["FAKE_WORKER_LOG"] = str(self.worker_log)

    def worker_calls(self):
        return [json.loads(line) for line in self.worker_log.read_text().splitlines()]


class PiWorkerTests(WorkerHarness):
    def test_review_uses_read_only_tools_and_the_isolated_profile(self):
        code, record = self.relay("run", "--worker", "pi", "--mode", "review", "--model", "anthropic/claude-sonnet-5-5",
                                  "--effort", "high", "--brief", "-")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["agent"], "pi")
        self.assertEqual(record["status"], "completed")
        call = self.worker_calls()[0]
        argv = call["argv"]
        self.assertEqual(call["stdin"], "Review README.md for typos.")
        self.assertEqual(self.flag(argv, "--mode"), "json")
        self.assertEqual(self.flag(argv, "--tools"), "read,grep,find,ls")
        self.assertEqual(self.flag(argv, "--thinking"), "high")
        for isolation in ("--no-extensions", "--no-skills", "--no-context-files", "--no-mcp", "--no-approve"):
            self.assertIn(isolation, argv)
        self.assertIn("```json", self.flag(argv, "--append-system-prompt"))
        self.assertEqual(record["claude"]["session_id"], self.flag(argv, "--session-id"))
        self.assertEqual(record["claude"]["models"], ["anthropic/claude-sonnet-5-5"])
        self.assertEqual(record["claude"]["cost_delta_usd"], 0.03)
        self.assertEqual(record["claimed"]["summary"], "Did the thing.")
        self.assertIs(record["observed"]["read_only_violation"], False)
        self.assertTrue(any("turn limit" in note for note in record["notes"]))
        self.assertFalse(self.log.exists())  # Claude was never called

    def test_implement_runs_in_a_worktree_without_a_shell_by_default(self):
        code, record = self.relay("run", "--worker", "pi", "--mode", "implement", "--worktree", "pi-fix", "--brief", "-",
                                  FAKE_WORKER_SCENARIO="edit")
        self.assertEqual(code, 0, record)
        self.assertEqual(self.flag(self.worker_calls()[0]["argv"], "--tools"), "read,grep,find,ls,edit,write")
        self.assertEqual(Path(self.worker_calls()[0]["cwd"]).resolve(), Path(record["worktree"]["path"]).resolve())
        self.assertEqual(record["observed"]["changed_files"], ["crew-was-here.txt"])
        self.assertFalse((self.repo / "crew-was-here.txt").exists())

    def test_scoped_bash_rules_need_an_explicit_unscoped_opt_in(self):
        code, result = self.relay("run", "--worker", "pi", "--mode", "implement", "--worktree", "pi-bash",
                                  "--allow", "Bash(pytest *)", "--brief", "-")
        self.assertEqual(code, 2, result)
        self.assertIn("--unscoped-bash", result["error"])
        self.assertNotIn("crew/pi-bash", self.git("branch", "--list"))
        code, record = self.relay("run", "--worker", "pi", "--mode", "implement", "--worktree", "pi-bash",
                                  "--unscoped-bash", "--brief", "-")
        self.assertEqual(code, 0, record)
        self.assertIn("bash", self.flag(self.worker_calls()[0]["argv"], "--tools").split(","))
        self.assertTrue(any("unrestricted bash" in note for note in record["notes"]))

    def test_limits_pi_cannot_enforce_are_refused(self):
        cases = [
            ["--mode", "review", "--max-budget-usd", "1"],
            ["--mode", "review", "--max-turns", "5"],
            ["--mode", "review", "--allow", "WebFetch"],
            ["--mode", "review", "--unscoped-bash"],
        ]
        for args in cases:
            code, result = self.relay("run", "--worker", "pi", "--brief", "-", *args)
            self.assertEqual(code, 2, args)
            self.assertEqual(result["status"], "usage_error", args)
        self.assertFalse(self.worker_log.exists())

    def test_resume_and_fork_keep_the_worker(self):
        _, first = self.relay("run", "--worker", "pi", "--mode", "review", "--brief", "-")
        code, second = self.relay("run", "--resume", first["run_id"], "--brief", "-", brief="Now check docs/.")
        self.assertEqual(code, 0, second)
        self.assertEqual(second["agent"], "pi")
        argv = self.worker_calls()[1]["argv"]
        self.assertEqual(self.flag(argv, "--session"), first["claude"]["session_id"])
        self.assertNotIn("--session-id", argv)
        self.assertEqual(second["claude"]["session_id"], first["claude"]["session_id"])
        _, forked = self.relay("run", "--resume", second["run_id"], "--fork", "--brief", "-")
        argv = self.worker_calls()[2]["argv"]
        self.assertEqual(self.flag(argv, "--fork"), first["claude"]["session_id"])
        self.assertEqual(forked["claude"]["session_id"], self.flag(argv, "--session-id"))
        self.assertNotEqual(forked["claude"]["session_id"], first["claude"]["session_id"])
        code, result = self.relay("run", "--resume", first["run_id"], "--worker", "opencode", "--brief", "-")
        self.assertEqual(code, 2, result)

    def test_failed_response_and_missing_report_are_errors(self):
        code, record = self.relay("run", "--worker", "pi", "--mode", "review", "--brief", "-", FAKE_WORKER_SCENARIO="error")
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "error")
        self.assertIn("429", record["error"])
        code, record = self.relay("run", "--worker", "pi", "--mode", "review", "--brief", "-",
                                  FAKE_WORKER_SCENARIO="no_report")
        self.assertEqual(code, 1)
        self.assertIsNone(record["claimed"])
        self.assertIn("structured report", record["error"])

    def test_calls_to_tools_outside_the_profile_are_recorded_as_denials(self):
        _, record = self.relay("run", "--worker", "pi", "--mode", "review", "--brief", "-",
                               FAKE_WORKER_SCENARIO="bash_denied")
        self.assertEqual(record["claude"]["permission_denials"], [{"tool": "bash", "input": "npm install"}])

    def test_timeout_stops_pi(self):
        code, record = self.relay("run", "--worker", "pi", "--mode", "review", "--timeout", "1", "--brief", "-",
                                  FAKE_WORKER_SCENARIO="sleep")
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "timed_out")

    def test_preflight_checks_version_and_never_echoes_credentials(self):
        code, report = self.relay("preflight", "--worker", "pi", "--model", "anthropic/claude-sonnet-5-5")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["version"], "1.2.0")
        self.assertIs(report["auth"]["loggedIn"], True)
        self.assertNotIn("sk-secret", json.dumps(report))
        code, report = self.relay("preflight", "--worker", "pi", FAKE_WORKER_VERSION="1.1.9")
        self.assertEqual(code, 1)
        self.assertFalse(report["capabilities"]["restricted_profile"])
        code, result = self.relay("run", "--worker", "pi", "--mode", "review", "--brief", "-", FAKE_WORKER_VERSION="1.1.9")
        self.assertEqual(result["status"], "worker_incompatible")
        code, result = self.relay("run", "--worker", "pi", "--mode", "review", "--brief", "-",
                                  CREW_PI_BIN=str(self.tmp / "nope"))
        self.assertEqual(code, 127)
        self.assertEqual(result["status"], "worker_unavailable")


class OpenCodeWorkerTests(WorkerHarness):
    def agent_config(self, call):
        config = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])
        (name, agent), = config["agent"].items()
        return name, agent

    def test_review_runs_a_deny_by_default_agent(self):
        code, record = self.relay("run", "--worker", "opencode", "--mode", "review", "--model", "anthropic/claude-sonnet-5-5",
                                  "--effort", "high", "--max-turns", "7", "--allow", "Bash(pytest *)", "--brief", "-")
        self.assertEqual(code, 0, record)
        self.assertEqual(record["agent"], "opencode")
        call = self.worker_calls()[0]
        argv = call["argv"]
        self.assertEqual(argv[:3], ["run", "--format", "json"])
        self.assertEqual(self.flag(argv, "--agent"), "codex-crew-review")
        self.assertEqual(self.flag(argv, "--model"), "anthropic/claude-sonnet-5-5")
        self.assertEqual(self.flag(argv, "--variant"), "high")
        self.assertTrue(call["stdin"].endswith("Review README.md for typos."))
        self.assertIn("```json", call["stdin"])
        for variable in ("OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_PURE", "OPENCODE_DISABLE_CLAUDE_CODE"):
            self.assertEqual(call["env"][variable], "1")
        name, agent = self.agent_config(call)
        self.assertEqual(name, "codex-crew-review")
        self.assertEqual(agent["steps"], 7)
        permission = agent["permission"]
        self.assertEqual(next(iter(permission)), "*")  # the catch-all comes first; later rules win
        self.assertEqual(permission["*"], "deny")
        self.assertNotIn("edit", permission)
        self.assertEqual(permission["external_directory"], "deny")
        bash = permission["bash"]
        self.assertEqual(list(bash)[0], "*")
        self.assertEqual(bash["*"], "deny")
        self.assertEqual(bash["pytest *"], "allow")
        self.assertEqual(bash["git diff *"], "allow")
        self.assertEqual(bash["git push *"], "deny")
        self.assertTrue(record["claude"]["session_id"].startswith("ses_"))
        self.assertEqual(record["claude"]["num_turns"], 1)
        self.assertEqual(record["claude"]["cost_delta_usd"], 0.02)
        self.assertEqual(record["claimed"]["status"], "complete")
        self.assertIs(record["observed"]["read_only_violation"], False)

    def test_implement_allows_edits_in_a_worktree(self):
        code, record = self.relay("run", "--worker", "opencode", "--mode", "implement", "--worktree", "oc-fix",
                                  "--brief", "-", FAKE_WORKER_SCENARIO="edit")
        self.assertEqual(code, 0, record)
        call = self.worker_calls()[0]
        name, agent = self.agent_config(call)
        self.assertEqual(name, "codex-crew-implement")
        self.assertEqual(agent["permission"]["edit"], "allow")
        self.assertEqual(Path(call["cwd"]).resolve(), Path(record["worktree"]["path"]).resolve())
        self.assertEqual(record["observed"]["changed_files"], ["crew-was-here.txt"])

    def test_resume_by_session_id_and_fork(self):
        _, first = self.relay("run", "--worker", "opencode", "--mode", "review", "--brief", "-")
        session = first["claude"]["session_id"]
        code, second = self.relay("run", "--resume", session, "--brief", "-", brief="Follow up.")
        self.assertEqual(code, 0, second)
        self.assertEqual(self.flag(self.worker_calls()[1]["argv"], "--session"), session)
        self.assertEqual(second["claude"]["session_id"], session)
        self.assertEqual(second["request"]["resume_from"]["run_id"], first["run_id"])
        _, forked = self.relay("run", "--resume", second["run_id"], "--fork", "--brief", "-")
        self.assertIn("--fork", self.worker_calls()[2]["argv"])
        self.assertNotEqual(forked["claude"]["session_id"], session)

    def test_permission_denials_and_errors_are_recorded(self):
        _, record = self.relay("run", "--worker", "opencode", "--mode", "review", "--brief", "-",
                               FAKE_WORKER_SCENARIO="denied")
        self.assertEqual(record["claude"]["permission_denials"], [
            {"tool": "bash", "input": "npm install"}, {"tool": "bash", "input": "rm -rf build"}])
        code, record = self.relay("run", "--worker", "opencode", "--mode", "review", "--brief", "-",
                                  FAKE_WORKER_SCENARIO="error")
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "error")
        self.assertIn("provider unavailable", record["error"])
        self.assertIn("ProviderModelNotFoundError", record["error"])  # the cause comes from OpenCode's log
        self.assertNotIn("trace", record["error"])
        self.assertIn("--print-logs", self.worker_calls()[1]["argv"])

    def test_budget_and_unscoped_bash_are_refused(self):
        for args in (["--max-budget-usd", "1"], ["--unscoped-bash"]):
            code, result = self.relay("run", "--worker", "opencode", "--mode", "review", "--brief", "-", *args)
            self.assertEqual(code, 2, args)
        self.assertFalse(self.worker_log.exists())

    def test_preflight_does_not_block_on_unconfirmable_sign_in(self):
        code, report = self.relay("preflight", "--worker", "opencode")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["version"], "1.19.0")
        self.assertIsNone(report["auth"])
        self.assertTrue(any("opencode auth list" in note for note in report["notes"]))
