"""Stand-in for the `claude` CLI, so relay tests never call a model.

FAKE_CLAUDE_SCENARIO picks the behavior: success (default), edit, error, garbage or sleep.
FAKE_CLAUDE_VERSION sets the reported version. Each run appends its argv, cwd and stdin
to the JSON-lines file named by FAKE_CLAUDE_LOG.
"""

import json
import os
import sys
import time
import uuid


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.201") + " (Claude Code)")
        return 0
    if argv[:2] == ["auth", "status"]:
        print(json.dumps({
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
            "email": "someone@example.com",
        }))
        return 0

    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "success")
    stdin = sys.stdin.read()
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
        log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "stdin": stdin}) + "\n")
    session_id = flag(argv, "--session-id") or flag(argv, "--resume")
    if "--fork-session" in argv:
        session_id = str(uuid.uuid4())

    if scenario == "sleep":
        time.sleep(60)
        return 0
    if scenario == "garbage":
        print("not json")
        print("error: unknown option '--json-schema'", file=sys.stderr)
        return 1
    if scenario == "error":
        print(json.dumps({
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "session_id": session_id,
            "num_turns": 50,
            "total_cost_usd": 0.5,
            "modelUsage": {},
            "permission_denials": [],
            "errors": ["Reached max turns (50)"],
        }))
        return 1
    if scenario == "edit":
        with open("crew-was-here.txt", "w") as fh:
            fh.write("hello from the fake claude\n")

    report = {
        "status": "complete",
        "summary": "Did the thing.",
        "findings": [],
        "changed_files": ["crew-was-here.txt"] if scenario == "edit" else [],
        "checks": [{"command": "pytest -q", "status": "passed"}],
        "open_issues": [],
    }
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": session_id,
        "num_turns": 3,
        "duration_ms": 1200,
        "total_cost_usd": 0.12,
        "result": "Did the thing.",
        "modelUsage": {"claude-sonnet-5-5": {}},
        "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "npm install"}}],
        "structured_output": report,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
