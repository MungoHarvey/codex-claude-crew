"""Stand-in for the `pi` CLI (Pi coding agent), so relay tests never call a model.

Emits the `pi --mode json` event shapes the relay reads: a session header, then
message, turn, tool and settle events. FAKE_WORKER_SCENARIO picks the behavior:
success (default), edit, error, no_report, bash_denied or sleep. Each run appends
its argv, cwd and stdin to the JSON-lines file named by FAKE_WORKER_LOG.
"""

import json
import os
import sys
import time

HELP_FLAGS = ["--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
              "--no-mcp", "--no-approve", "--offline", "--session-id", "--fork", "--mode", "--tools", "--thinking"]


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def emit(event):
    print(json.dumps(event), flush=True)


def assistant(text, stop="stop", error=None, cost=0.03):
    message = {"role": "assistant", "content": [{"type": "text", "text": text}], "provider": "anthropic",
               "model": "claude-sonnet-5-5", "stopReason": stop,
               "usage": {"input": 10, "output": 5, "cost": {"total": cost}}}
    if error:
        message["errorMessage"] = error
    return message


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print(os.environ.get("FAKE_WORKER_VERSION", "1.2.0"))
        return 0
    if "--help" in argv:
        print("pi - AI coding assistant\n" + "\n".join(HELP_FLAGS))
        return 0
    if argv[:2] == ["auth", "check"]:
        ready = not os.environ.get("FAKE_WORKER_LOGGED_OUT")
        print(json.dumps({"status": "ready" if ready else "missing", "provider": "anthropic",
                          "authType": "api_key", "credential": "sk-secret"}))
        return 0 if ready else 1

    scenario = os.environ.get("FAKE_WORKER_SCENARIO", "success")
    stdin = sys.stdin.read()
    with open(os.environ["FAKE_WORKER_LOG"], "a") as log:
        log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "stdin": stdin}) + "\n")
    session_id = flag(argv, "--session-id") or flag(argv, "--session")
    emit({"type": "session", "version": 3, "id": session_id, "cwd": os.getcwd()})
    emit({"type": "agent_start"})
    if scenario == "sleep":
        time.sleep(60)
    if scenario == "edit":
        with open("crew-was-here.txt", "w") as fh:
            fh.write("hello from the fake pi\n")
    if scenario == "bash_denied":
        emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
              "args": {"command": "npm install"}, "result": {"content": [{"type": "text", "text": "Tool bash not found"}]},
              "isError": True})
    emit({"type": "turn_end", "message": {}, "toolResults": []})
    if scenario == "error":
        emit({"type": "message_end", "message": assistant("", stop="error", error="429 rate limited")})
        emit({"type": "agent_settled", "aborted": False})
        return 0  # Pi's JSON mode exits 0 even when the response failed
    report = {"status": "complete", "summary": "Did the thing.", "findings": [],
              "changed_files": ["crew-was-here.txt"] if scenario == "edit" else [],
              "checks": [], "open_issues": []}
    text = "All done." if scenario == "no_report" else "Done.\n\n```json\n" + json.dumps(report) + "\n```"
    emit({"type": "message_end", "message": assistant(text, cost=float(os.environ.get("FAKE_WORKER_COST", "0.03")))})
    emit({"type": "turn_end", "message": {}, "toolResults": []})
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled", "aborted": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
