"""Stand-in for the `opencode` CLI, so relay tests never call a model.

Emits the `opencode run --format json` event shapes the relay reads: text,
tool_use, step_finish and error records, each carrying the sessionID. It also
prints the plain-text line `opencode run` writes when it auto-rejects a
permission prompt. FAKE_WORKER_SCENARIO picks the behavior: success (default),
edit, error, denied or no_report. Each run appends its argv, cwd, stdin and
OpenCode environment settings to the JSON-lines file named by FAKE_WORKER_LOG.
"""

import json
import os
import sys
import uuid

HELP_FLAGS = ["--format", "--agent", "--session", "--fork", "--variant", "--model", "--print-logs"]


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print(os.environ.get("FAKE_WORKER_VERSION", "1.19.0"))
        return 0
    if "--help" in argv:
        print("opencode run [message..]\n" + "\n".join(HELP_FLAGS))
        return 0
    assert argv[:1] == ["run"], argv

    scenario = os.environ.get("FAKE_WORKER_SCENARIO", "success")
    stdin = sys.stdin.read()
    env = {key: value for key, value in os.environ.items() if key.startswith("OPENCODE_")}
    with open(os.environ["FAKE_WORKER_LOG"], "a") as log:
        log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "stdin": stdin, "env": env}) + "\n")
    session_id = flag(argv, "--session") or "ses_" + uuid.uuid4().hex[:24]
    if "--fork" in argv:
        session_id = "ses_" + uuid.uuid4().hex[:24]

    def emit(kind, **fields):
        print(json.dumps({"type": kind, "timestamp": 0, "sessionID": session_id, **fields}), flush=True)

    emit("step_start", part={"type": "step-start"})
    if scenario == "edit":
        with open("crew-was-here.txt", "w") as fh:
            fh.write("hello from the fake opencode\n")
    if scenario == "denied":
        print("! permission requested: bash (npm install); auto-rejecting", flush=True)
        emit("tool_use", part={"type": "tool", "tool": "bash", "state": {
            "status": "error", "input": {"command": "rm -rf build"},
            "error": "The user has specified a rule which prevents you from using this specific tool call."}})
    if scenario == "error":
        print('level=ERROR message=failed error="ProviderModelNotFoundError: Model not found" cause="trace"',
              file=sys.stderr)
        emit("error", error={"name": "APIError", "data": {"message": "provider unavailable"}})
        return 1
    report = {"status": "complete", "summary": "Did the thing.", "findings": [],
              "changed_files": ["crew-was-here.txt"] if scenario == "edit" else [],
              "checks": [], "open_issues": []}
    text = "All done." if scenario == "no_report" else "```json\n" + json.dumps(report) + "\n```"
    emit("text", part={"type": "text", "text": text, "time": {"end": 1}})
    emit("step_finish", part={"type": "step-finish", "reason": "stop",
                              "cost": float(os.environ.get("FAKE_WORKER_COST", "0.02")), "tokens": {}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
