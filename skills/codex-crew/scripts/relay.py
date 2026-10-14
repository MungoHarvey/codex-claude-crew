#!/usr/bin/env python3
"""Run one bounded task in a local coding-agent session for Codex.

Codex calls this script instead of building worker commands by hand. The worker is
Claude Code (`claude -p`, the default), Pi (`pi --mode json`) or OpenCode
(`opencode run --format json`). The relay sets the permission boundary for the
chosen mode, creates the git worktree for implement runs, captures the worker's
JSON output, and writes a run record that keeps the worker's claims apart from
Codex's own verification.

Standard library only. Windows, macOS and Linux.

  relay.py preflight [--worker W]       check the worker CLI and sign-in (no model call)
  relay.py workers                      check which of claude, pi and opencode are usable
  relay.py run --mode review ...        start a run (see `relay.py run --help`)
  relay.py status RUN [--wait SECONDS]  print a run record
  relay.py list [--limit N] [--thread]  list recent runs
  relay.py verify RUN --verdict V ...   record Codex's own verification
  relay.py cancel RUN                   stop a running run

Environment: CLAUDE_CREW_HOME (state folder, default ~/.codex-claude-crew), and
CLAUDE_CREW_BIN, CREW_PI_BIN and CREW_OPENCODE_BIN (paths to the worker
executables, default: `claude`, `pi` and `opencode` found on PATH).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import ctypes
import ctypes.wintypes
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

RECORD_SCHEMA = "codex-claude-crew.run/v1"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
VERDICTS = ("confirmed", "partial", "rejected")
ACTIVE = ("queued", "running", "stopping", "orphaned")
STOP_SIGNALS = ((signal.SIGTERM, signal.SIGINT, signal.SIGBREAK) if os.name == "nt"
                else (signal.SIGTERM, signal.SIGINT, signal.SIGHUP))
# Require both restricted mode and unattended permission denial; no weaker fallback.
MIN_CLAUDE_VERSION = (2, 1, 259)
PROFILE_FLAGS = ["--restricted", "--permission-prompts", "none", "--no-chrome",
                 "--disable-slash-commands", "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}']
DEFAULT_REQUEST = {"model": None, "effort": None, "max_turns": 50,
                   "max_budget_usd": None, "timeout_s": 1800, "allow": []}

# Pi: skip extensions, skills, prompt templates, themes, MCP, AGENTS.md/CLAUDE.md
# and project-local .pi resources, and make no startup network calls.
PI_PROFILE_FLAGS = ["--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
                    "--no-context-files", "--no-mcp", "--no-approve", "--offline"]
PI_READ_TOOLS = ["read", "grep", "find", "ls"]
# Pi has no permission rules, so this extension blocks bash commands outside the
# run's rules. It loads with -e, which still works under --no-extensions.
PI_GATE = Path(__file__).resolve().with_name("pi_command_gate.js")
PI_GATE_BLOCKED = "codex-crew: command not allowed"  # sent to the gate with its rules
# OpenCode: skip project config, AGENTS.md lookup, plugins, .claude prompts/skills,
# external skills, sharing and update checks. The permission boundary is a named
# agent passed in OPENCODE_CONFIG_CONTENT; agent rules are evaluated after the
# user's global rules, so its catch-all deny wins.
OPENCODE_PROFILE_ENV = {
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_PURE": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_SHARE": "1",
}
OPENCODE_RUN_FLAGS = ("--format", "--agent", "--session", "--fork", "--variant", "--model", "--print-logs")
OPENCODE_AGENT = "codex-crew-{mode}"
OPENCODE_READ_PERMISSIONS = {
    "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
    "glob": "allow", "grep": "allow", "list": "allow", "todowrite": "allow", "todoread": "allow",
}

# What differs per worker. Command building and output reading branch on the
# worker in worker_command() and read_result(). Pi and OpenCode minimum versions
# are the releases their profiles were checked against; older ones are refused
# rather than guessed at. `limits` says which request limits the worker enforces;
# `preset_session` says when the relay chooses the session id itself.
WORKER_SPECS = {
    "claude": {
        "bin_env": "CLAUDE_CREW_BIN", "min_version": MIN_CLAUDE_VERSION,
        # Claude's help can omit valid flags, so it parses them all instead.
        "probe": [*PROFILE_FLAGS, "--max-turns", "1", "--json-schema", "{}", "--help"], "help_flags": None,
        "limits": {"budget": True, "turns": True, "web": True}, "preset_session": ("new",),
        "unavailable": "claude_unavailable", "incompatible": "claude_incompatible", "stdout": "claude.stdout.json",
        "install": "Install Claude Code: https://code.claude.com/docs/en/getting-started",
        "sign_in": "Run `claude` once and sign in.",
    },
    "pi": {
        "bin_env": "CREW_PI_BIN", "min_version": (1, 2, 0),
        "probe": ["--help"],
        "help_flags": [*PI_PROFILE_FLAGS, "--session-id", "--fork", "--mode", "--tools", "--thinking", "--extension"],
        "limits": {"budget": False, "turns": False, "web": False}, "preset_session": ("new", "fork"),
        "unavailable": "worker_unavailable", "incompatible": "worker_incompatible", "stdout": "pi.stdout.jsonl",
        "install": "npm install -g @earendil-works/pi-coding-agent",
        "sign_in": "Run `pi` and use /login, or set the provider's API key (e.g. ANTHROPIC_API_KEY).",
    },
    "opencode": {
        "bin_env": "CREW_OPENCODE_BIN", "min_version": (1, 19, 0),
        "probe": ["run", "--help"], "help_flags": list(OPENCODE_RUN_FLAGS),
        # OpenCode assigns its own session id, which arrives in the event stream.
        "limits": {"budget": False, "turns": True, "web": True}, "preset_session": (),
        "unavailable": "worker_unavailable", "incompatible": "worker_incompatible", "stdout": "opencode.stdout.jsonl",
        "install": "npm install -g opencode-ai",
        "sign_in": "Run `opencode providers login`, then check with `opencode providers list`.",
    },
}
WORKERS = tuple(WORKER_SPECS)

READ_TOOLS = ["Read", "Glob", "Grep"]
EXTRA_TOOLS = ("Bash", "WebFetch", "WebSearch")  # what --allow may add
GIT_READ_RULES = [
    "Bash(git diff *)",
    "Bash(git log *)",
    "Bash(git show *)",
    "Bash(git status *)",
    "Bash(git blame *)",
]
DENY_RULES = ["Bash(git commit *)", "Bash(git push *)", "mcp__*"]

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
RUN_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
# Claude and Pi session ids are UUIDs; OpenCode's look like ses_...
SESSION_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}|ses_[A-Za-z0-9]{8,64}")
MODEL_RE = re.compile(r"[A-Za-z0-9._:@/\[\]-]{1,100}")
RULE_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*)(\(.+\))?")

# Passed with --json-schema, so Claude's final answer arrives as structured_output.
REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["complete", "partial", "blocked"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low", "info"]},
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "detail": {"type": "string"},
                },
                "required": ["severity", "title", "detail"],
            },
        },
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string"},
                    "status": {"type": "string", "enum": ["passed", "failed", "not_run", "denied"]},
                    "notes": {"type": "string"},
                },
                "required": ["command", "status"],
            },
        },
        "open_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "findings", "changed_files", "checks", "open_issues"],
}

# Appended to Claude Code's system prompt, so the brief can stay about the task.
WORKER_RULES = """\
You are a worker session started by Codex, another coding agent, through codex-crew. \
Nobody is watching this session: no one can answer questions or approve permission prompts.
- Work only on the task in the brief, inside the working directory.
- Do not commit, push, rebase, reset or switch branches. Codex reviews and lands the work.
- If you are blocked or the brief is ambiguous, stop and say so in the report instead of guessing.
- Report checks honestly. Mark a check passed only if you ran it and saw it pass; mark denied commands as denied."""

MODE_RULES = {
    "review": "Mode: review. This run is read-only: do not create, edit or delete files. "
    "Put each issue in `findings`, with a file path and line where you can. Leave `changed_files` empty.",
    "implement": "Mode: implement. You are in a git worktree made for this task. "
    "Make the change the brief asks for and keep the diff focused on it. "
    "List every file you changed in `changed_files`.",
}

# Pi and OpenCode have no schema-constrained output, so the report contract is
# stated in the prompt and the relay validates the JSON it gets back.
REPORT_RULES = """\
Your final message is your report. It must contain only one JSON object in a ```json fenced block, \
matching this JSON Schema, with nothing after the block:
""" + json.dumps(REPORT_SCHEMA, separators=(",", ":"))


class Fail(Exception):
    """Stops a command: prints {"status", "error"} as JSON and exits with `code`."""

    def __init__(self, message, code=2, status="usage_error"):
        super().__init__(message)
        self.code = code
        self.status = status


class Interrupted(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def emit(payload, code=0):
    print(json.dumps(payload, indent=2))
    return code


def crew_home():
    # The folder, env var and record schema keep their codex-claude-crew names so
    # runs and worktrees from before the codex-crew rename stay visible.
    return Path(os.environ.get("CLAUDE_CREW_HOME") or Path.home() / ".codex-claude-crew").expanduser().resolve()


def runs_dir():
    return crew_home() / "runs"


# --- worker CLIs ------------------------------------------------------------------


def record_worker(record):
    return record.get("agent", "claude")  # records from before Pi and OpenCode have no agent


def worker_bin(kind):
    configured = os.environ.get(WORKER_SPECS[kind]["bin_env"])
    if configured:
        path = Path(configured).expanduser().resolve()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(kind)


def cli_version(binary):
    try:
        proc = subprocess.run(command_for(binary, ["--version"]), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\d+\.\d+\.\d+", proc.stdout)
    return match.group(0) if match else None


def version_tuple(version):
    return tuple(int(part) for part in version.split(".")) if version else ()


def capabilities(binary, kind):
    """A version floor plus the flags the worker's profile depends on, without a model call."""
    spec = WORKER_SPECS[kind]
    version = cli_version(binary)
    supported = version_tuple(version) >= spec["min_version"]
    flags_ok = False
    if supported:
        try:
            proc = subprocess.run(command_for(binary, spec["probe"]), capture_output=True, text=True, timeout=30)
            if spec["help_flags"] is None:
                flags_ok = proc.returncode == 0 and not re.search(
                    r"unknown option|unexpected option|unrecognized", proc.stderr, re.I)
            else:
                flags_ok = proc.returncode == 0 and all(f in proc.stdout + proc.stderr for f in spec["help_flags"])
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"version": version, "minimum_version": ".".join(map(str, spec["min_version"])),
            "version_supported": supported, "restricted_profile": bool(flags_ok)}


def require_worker(kind):
    """(binary, capabilities), or a Fail that says what to install or update."""
    spec = WORKER_SPECS[kind]
    binary = worker_bin(kind)
    if not binary:
        raise Fail(f"{kind} not found. Install it and sign in, or set {spec['bin_env']}.", 127, spec["unavailable"])
    caps = capabilities(binary, kind)
    if not caps["restricted_profile"]:
        raise Fail(f"{kind} {caps['minimum_version']}+ with the relay's profile flags is required; update it",
                   1, spec["incompatible"])
    return binary, caps


def bash_pattern(rule):
    """`Bash(npm test *)` becomes the pattern `npm test *`."""
    return rule[len("Bash("):-1].strip()


def bash_patterns(request):
    """(allow, deny) command patterns for Pi's gate and OpenCode's agent: read-only git
    plus the run's --allow commands, never the commands DENY_RULES forbids."""
    allow = [bash_pattern(rule) for rule in [*GIT_READ_RULES, *request["allow"]] if rule.startswith("Bash(")]
    deny = [bash_pattern(rule) for rule in DENY_RULES if rule.startswith("Bash(")]
    return allow, deny


def claude_args(record):
    """The `claude -p` flags for a run. The brief itself goes in on stdin."""
    mode, request = record["mode"], record["request"]
    if mode == "review":
        tools = READ_TOOLS + ["Bash"]
        allowed = READ_TOOLS + GIT_READ_RULES
        permission_mode = "dontAsk"  # anything not pre-approved or read-only is denied
    else:
        tools = READ_TOOLS + ["Edit", "Write", "Bash"]
        allowed = READ_TOOLS + ["Edit", "Write"] + GIT_READ_RULES
        permission_mode = "acceptEdits"  # file edits pass; other commands need an --allow rule
    for rule in request["allow"]:
        tool = rule.split("(", 1)[0]
        if tool not in tools:
            tools.append(tool)
    args = [
        "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(REPORT_SCHEMA, separators=(",", ":")),
        "--append-system-prompt", f"{WORKER_RULES}\n\n{MODE_RULES[mode]}",
        "--permission-mode", permission_mode,
        "--tools", ",".join(tools),
        "--allowedTools", *allowed, *request["allow"],
        "--disallowedTools", ",".join(DENY_RULES),
        *PROFILE_FLAGS,
        "--max-turns", str(request["max_turns"]),
    ]
    if request["max_budget_usd"] is not None:
        args += ["--max-budget-usd", str(request["max_budget_usd"])]
    if request["model"]:
        args += ["--model", request["model"]]
    if request["effort"]:
        args += ["--effort", request["effort"]]
    if request["resume_from"]:
        args += ["--resume", request["resume_from"]["session_id"]]
        if request["fork"]:
            args.append("--fork-session")
    else:
        # Choosing the id up front means a run that times out can still be resumed.
        args += ["--session-id", record["claude"]["session_id"]]
    return args


def pi_tools(mode):
    """bash is always present; PI_GATE decides which commands it may run."""
    return PI_READ_TOOLS + (["edit", "write"] if mode == "implement" else []) + ["bash"]


def pi_args(record):
    mode = record["mode"]
    rules = [WORKER_RULES, MODE_RULES[mode],
             "Shell commands outside the allowed set are blocked; mark them `denied` in `checks`. "
             "Run each command on its own, without redirecting output to files or substituting commands.",
             REPORT_RULES]
    args = ["--mode", "json", *PI_PROFILE_FLAGS, "--extension", str(PI_GATE),
            "--tools", ",".join(pi_tools(mode)),
            "--append-system-prompt", "\n\n".join(rules)]
    request = record["request"]
    if request["model"]:
        args += ["--model", request["model"]]
    if request["effort"]:
        args += ["--thinking", request["effort"]]
    if request["resume_from"] and request["fork"]:
        args += ["--fork", request["resume_from"]["session_id"], "--session-id", record["claude"]["session_id"]]
    elif request["resume_from"]:
        args += ["--session", request["resume_from"]["session_id"]]
    else:
        args += ["--session-id", record["claude"]["session_id"]]
    return args


def opencode_agent(record):
    """The agent definition that carries the permission boundary for one run."""
    mode, request = record["mode"], record["request"]
    allow, deny = bash_patterns(request)
    # Last match wins, so the catch-all comes first and the denials last.
    bash = {"*": "deny", **dict.fromkeys(allow, "allow"), **dict.fromkeys(deny, "deny")}
    permission = {"*": "deny", **OPENCODE_READ_PERMISSIONS, "bash": bash, "external_directory": "deny"}
    if mode == "implement":
        permission["edit"] = "allow"
    for tool in ("WebFetch", "WebSearch"):
        if tool in request["allow"]:
            permission[tool.lower()] = "allow"
    return {"mode": "primary", "description": f"codex-crew {mode} worker",
            "steps": request["max_turns"], "permission": permission}


def opencode_args(record):
    request = record["request"]
    # Error logs go to stderr: OpenCode's JSON error events often say only "Unexpected server error".
    args = ["run", "--format", "json", "--agent", OPENCODE_AGENT.format(mode=record["mode"]),
            "--print-logs", "--log-level", "ERROR"]
    if request["model"]:
        args += ["--model", request["model"]]
    if request["effort"]:
        args += ["--variant", request["effort"]]
    if request["resume_from"]:
        args += ["--session", request["resume_from"]["session_id"]]
        if request["fork"]:
            args.append("--fork")
    return args


def worker_command(record, binary):
    """(argv, environment overrides, text sent ahead of the brief) for a run."""
    kind = record_worker(record)
    if kind == "claude":
        return [binary, *claude_args(record)], {}, ""
    if kind == "pi":
        if not PI_GATE.is_file():
            raise Fail(f"the Pi command gate is missing ({PI_GATE}); reinstall the skill", 1, "error")
        allow, deny = bash_patterns(record["request"])
        rules = {"allow": allow, "deny": deny, "marker": PI_GATE_BLOCKED}
        return [binary, *pi_args(record)], {"CODEX_CREW_PI_RULES": json.dumps(rules, separators=(",", ":"))}, ""
    config = {"$schema": "https://opencode.ai/config.json",
              "agent": {OPENCODE_AGENT.format(mode=record["mode"]): opencode_agent(record)}}
    env = {**OPENCODE_PROFILE_ENV, "OPENCODE_CONFIG_CONTENT": json.dumps(config, separators=(",", ":"))}
    # OpenCode keeps its own system prompt; the worker contract leads the message.
    prefix = "\n\n".join([WORKER_RULES, MODE_RULES[record["mode"]], REPORT_RULES, "Brief:"]) + "\n\n"
    return [binary, *opencode_args(record)], env, prefix


def transcript_path(cwd, session_id, kind):
    """Where the worker keeps the session. OpenCode stores sessions in a database instead."""
    if kind == "pi":
        root = Path(os.environ.get("PI_CODING_AGENT_SESSION_DIR") or
                    Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent") / "sessions")
        match = max(root.glob(f"*/*{session_id}*.jsonl"), default=None) if root.is_dir() else None
        return str(match) if match else None
    if kind != "claude":
        return None
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    for folder in (cwd, os.path.realpath(cwd)):
        path = projects / re.sub(r"[^A-Za-z0-9]", "-", folder) / f"{session_id}.jsonl"
        if path.is_file():
            return str(path)
    return None


# --- git -----------------------------------------------------------------------


def git(cwd, *args, check=True):
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Fail(f"could not run git: {exc}", code=1, status="error")
    if check and proc.returncode != 0:
        raise Fail(f"git {' '.join(args)} failed: {proc.stderr.strip()}", code=1, status="error")
    return proc.stdout


def git_root(cwd):
    if not shutil.which("git"):
        return None
    root = git(cwd, "rev-parse", "--show-toplevel", check=False).strip()
    return Path(root) if root else None


def git_status(root):
    """Changed paths in the working tree and index, as {path: two-letter status}."""
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries, parts = {}, iter(out.split("\0"))
    for item in parts:
        if len(item) < 4:
            continue
        code, path = item[:2], item[3:]
        if "R" in code or "C" in code:
            next(parts, None)  # renames and copies carry the original path next
        entries[path] = code
    return entries


def fingerprint(root):
    """Snapshot HEAD, index and contents of Git-visible paths, including clean files.

    Ignored files and changes outside this repository are deliberately out of scope.
    A submodule or unreadable path makes coverage incomplete, never 'unchanged'.
    """
    result = {"head": git(root, "rev-parse", "--verify", "HEAD", check=False).strip(),
              "ref": git(root, "symbolic-ref", "-q", "HEAD", check=False).strip(),
              "status": git_status(root), "paths": {}, "index": {}, "complete": True}
    for entry in git(root, "ls-files", "--stage", "-z").split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            result["index"].setdefault(path, []).append(metadata)
    paths = set(result["index"]) | set(result["status"])
    for path in sorted(paths):
        target = root / path
        try:
            if target.is_symlink():
                value = "symlink:" + os.readlink(target)
            elif target.is_file():
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                value = f"{target.stat().st_mode & 0o777}:{digest.hexdigest()}"
            elif not target.exists():
                value = "missing"
            else:
                value = None
                result["complete"] = False
            result["paths"][path] = value
        except OSError:
            result["paths"][path] = None
            result["complete"] = False
    return result


def create_worktree(cwd, name, base):
    if not NAME_RE.fullmatch(name):
        raise Fail("--worktree takes a short name: lowercase letters, digits, '.', '_' or '-'")
    root = git_root(cwd)
    if not root:
        raise Fail(f"implement runs need a git repository; {cwd} isn't in one")
    base_sha = git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False).strip()
    if not base_sha:
        raise Fail(f"--base {base!r} isn't a commit in {root}")
    repo_key = f"{root.name}-{hashlib.sha1(str(root).encode()).hexdigest()[:8]}"
    path = crew_home() / "worktrees" / repo_key / name
    if path.exists():
        raise Fail(f"{path} already exists; continue it with --resume RUN_ID or pick another name")
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"crew/{name}"
    git(root, "worktree", "add", "-b", branch, str(path), base_sha)
    notes = []
    if git_status(root):
        notes.append("The main checkout has uncommitted changes. The worktree starts from the base commit without them.")
    return {"repo": str(root), "path": str(path), "branch": branch, "base": base_sha}, notes


# --- run records -----------------------------------------------------------------


@contextmanager
def state_lock():
    """Serialize short state transactions across CLI processes."""
    crew_home().mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(crew_home() / ".lock", "a+b") as lock:
        if os.name == "nt":
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def record_path(run_id):
    return runs_dir() / run_id / "run.json"


def save(record):
    """Atomic replacement. Callers serialize read/modify/write with state_lock."""
    path = record_path(record["run_id"])
    fd, temporary = tempfile.mkstemp(prefix="run-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(run_id):
    return json.loads(record_path(run_id).read_text())


def all_records():
    """Every saved record, newest first."""
    records = []
    for path in runs_dir().glob("*/run.json"):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(records, key=lambda record: record["created_at"], reverse=True)


def find_run(ref):
    """Look a run up by run id, or by worker session id (newest run of that session)."""
    if RUN_ID_RE.fullmatch(ref) and record_path(ref).is_file():
        return load(ref)
    if SESSION_RE.fullmatch(ref):
        for record in all_records():
            if record["claude"].get("session_id") == ref:
                return record
    return None


def require_run(ref):
    record = find_run(ref)
    if not record:
        raise Fail(f"no run found for {ref!r}")
    return record


def process_table():
    """Read process identities without command lines or environment values."""
    if os.name == "nt":
        return windows_process_table()
    proc = subprocess.run(["ps", "-axo", "pid=,pgid=,stat=,lstart="],
                          capture_output=True, text=True, timeout=10,
                          env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    if proc.returncode:
        raise Fail("could not inspect processes; execution state is unconfirmed", 1, "error")
    rows = {}
    for line in proc.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4 and "Z" not in fields[2]:
            rows[int(fields[0])] = {"pid": int(fields[0]), "pgid": int(fields[1]),
                                    "started": " ".join(fields[3].split())}
    return rows


def process_group_kwargs(detached=False):
    """Create an isolated worker process group using the host's native API."""
    if os.name == "nt":
        return {"creationflags": subprocess.DETACHED_PROCESS if detached else subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def python_executable():
    """Return the real interpreter, bypassing Windows venv redirector stubs."""
    if os.name == "nt":
        base = getattr(sys, "_base_executable", None)
        if base and Path(base).is_file():
            return base
    return sys.executable


def command_for(binary, args):
    """Build a native process command, safely supporting npm's Windows shims."""
    if os.name == "nt" and Path(binary).suffix.lower() in (".cmd", ".bat"):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        script = str(Path(binary).with_suffix(".ps1"))
        if not powershell or not Path(script).is_file():
            raise Fail(f"{binary} is a Windows command shim but its matching PowerShell launcher is missing")
        return [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", script, *args]
    return [binary, *args]


def create_windows_job(run_id):
    """Create a named, kill-on-close Job Object for a run's entire process tree."""
    from ctypes import wintypes

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
                     "WriteTransferCount", "OtherTransferCount")]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    name = rf"Local\CodexCrew_{run_id}"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateJobObjectW(None, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    limits = EXTENDED_LIMIT()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "SetInformationJobObject failed")
    return handle, name


def assign_windows_job(handle, proc):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    if not kernel32.AssignProcessToJobObject(handle, ctypes.c_void_p(int(proc._handle))):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")


def windows_job_process_ids(handle):
    """Return process IDs still assigned to a Job Object."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                   ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.QueryInformationJobObject.restype = ctypes.c_int
    size = 65536
    buffer = ctypes.create_string_buffer(size)
    returned = ctypes.c_ulong()
    if not kernel32.QueryInformationJobObject(handle, 3, buffer, size, ctypes.byref(returned)):
        raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
    assigned, listed = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ulong * 2)).contents
    count = min(int(assigned), int(listed))
    array_type = ctypes.c_size_t * count
    ids = ctypes.cast(ctypes.byref(buffer, 8), ctypes.POINTER(array_type)).contents
    return {int(pid) for pid in ids}


def terminate_windows_job(identity, handle=None):
    """Terminate and confirm all processes in a recorded Windows Job Object."""
    from ctypes import wintypes

    owned_handle = handle is None
    if handle is None:
        name = identity.get("job_name")
        if not name:
            return None  # old records use the process-tree fallback
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.OpenJobObjectW(0x0004 | 0x0008, False, name)  # QUERY | TERMINATE
        if not handle:
            return process_identity(identity["pid"]) is None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        if not kernel32.TerminateJobObject(handle, 1):
            return False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if not windows_job_process_ids(handle):
                    return True
            except OSError:
                return False
            time.sleep(0.1)
        return False
    finally:
        if owned_handle:
            kernel32.CloseHandle(handle)


def windows_process_table(include_start_times=False):
    """Enumerate process IDs and parents, optionally adding creation times."""
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                         ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value or not snapshot:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    rows = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            started = windows_process_started(pid) if include_start_times else None
            rows[pid] = {"pid": pid, "pgid": pid, "ppid": int(entry.th32ParentProcessID), "started": started}
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def windows_process_started(pid):
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                         ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return None
    finally:
        kernel32.CloseHandle(handle)


def process_identity(pid):
    row = process_table().get(pid)
    if row and os.name == "nt":
        row["started"] = windows_process_started(pid)
    return row


def same_process(identity, table=None):
    if not identity:
        return False
    actual = (process_table() if table is None else table).get(identity["pid"])
    if actual and os.name == "nt":
        actual = dict(actual, started=windows_process_started(identity["pid"]))
    return actual is not None and actual["started"] == identity["started"]


def group_members(pgid, started=None):
    if os.name == "nt":
        table = windows_process_table(include_start_times=True)
        root = table.get(pgid)
        root_started = root["started"] if root else started
        if root_started is None:
            return {}
        members, parents = {}, {pgid}
        changed = True
        while changed:
            changed = False
            for pid, row in table.items():
                if pid in parents or row["ppid"] not in parents:
                    continue
                # Avoid attaching a reused PID's unrelated descendants to an old run.
                if row["started"] is None or row["started"] < root_started:
                    continue
                members[pid] = row
                parents.add(pid)
                changed = True
        if root:
            members[pgid] = root
        return members
    return {pid: row for pid, row in process_table().items() if row["pgid"] == pgid}


def terminate_windows_tree(identity, owned=False):
    """Stop a verified Windows process tree; never act on a reused root PID."""
    stopped = terminate_windows_job(identity)
    if stopped is not None:
        return stopped
    pid = identity["pid"]
    table = process_table()
    root = table.get(pid)
    if root:
        root = dict(root, started=windows_process_started(pid))
    if root and root["started"] != identity["started"]:
        return False
    if not root and not owned:
        return False
    members = group_members(pid, identity.get("started"))
    if not members:
        return True
    # Best-effort graceful stop for processes in the isolated console group.
    if root:
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError, SystemError):
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not group_members(pid, identity.get("started")):
            return True
        time.sleep(0.1)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
    kernel32.TerminateProcess.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    # Terminate leaves preserve the verified parent chain until all descendants stop.
    for member_pid, member in sorted(members.items(), key=lambda item: item[0] == pid):
        current = process_table().get(member_pid)
        if current is None:
            continue
        if current["started"] != member["started"] or current["started"] is None:
            return False
        handle = kernel32.OpenProcess(0x0001, False, member_pid)  # PROCESS_TERMINATE
        if not handle:
            return False
        try:
            if not kernel32.TerminateProcess(handle, 1):
                return False
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not group_members(pid, identity.get("started")):
            return True
        time.sleep(0.1)
    return not group_members(pid, identity.get("started"))


def stop_group(identity, proc=None, owned=False, job_handle=None):
    """Stop the entire group, even after its leader exits.

    During recovery, refuse to signal a reused PID or an unidentifiable orphan
    group. Such jobs retain their reservation and require manual inspection.
    """
    if not identity:
        return False
    if os.name == "nt":
        stopped = terminate_windows_job(identity, handle=job_handle) if job_handle is not None else None
        if stopped is None:
            stopped = terminate_windows_tree(identity, owned=owned)
        if proc is not None:
            proc.poll()
        return stopped
    pgid = identity["pid"]
    members = group_members(pgid)
    if not members:
        return True
    leader = process_table().get(pgid)
    if leader and not same_process(identity, {pgid: leader}):
        return False
    if not leader and not owned:
        return False
    for sig, grace in ((signal.SIGINT, 5), (signal.SIGTERM, 3), (signal.SIGKILL, 2)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc is not None:
                proc.poll()  # reap the leader; zombies cannot do any more work
            if not group_members(pgid):
                return True
            time.sleep(0.1)
    return not group_members(pgid)


def refresh(record):
    """Reconcile lost workers; never release a live or unconfirmed execution."""
    with state_lock():
        record = load(record["run_id"])
        if record["status"] not in ACTIVE:
            return record
        table = process_table()
        if same_process(record.get("worker"), table):
            return record
        if record["status"] == "queued" and same_process(record.get("launcher"), table):
            return record
        if same_process(record.get("recovery_owner"), table):
            return record
        record.update(status="stopping", recovery_owner=process_identity(os.getpid()))
        save(record)
    child = record.get("child")
    # With the gated launch protocol an unrecorded child cannot start Claude.
    stopped = stop_group(child) if child else record.get("gated_launch", False)
    if stopped:
        baseline = record.get("baseline") or {}
        observe(record, Path(record["git_root"]) if record.get("git_root") else None, baseline)
        return finish(record, "error", "the relay worker was lost; execution has stopped")
    return finish(record, "orphaned", "worker lost; execution could not be confirmed stopped; inspect the recorded worker process tree")


def finish(record, status, error):
    session_id = record["claude"]["session_id"]
    if session_id:
        record["claude"]["transcript"] = transcript_path(record["cwd"], session_id, record_worker(record))
    record.update(status=status, error=error,
                  finished_at=None if status in ACTIVE else now(), recovery_owner=None)
    with state_lock():
        current = load(record["run_id"])
        # A late writer must not resurrect or overwrite a terminal record.
        if current["status"] not in ACTIVE:
            return current
        save(record)
    return record


# --- running Claude -----------------------------------------------------------------


def _interrupt(signum, frame):
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise Interrupted()


def parse_result(stdout):
    """The `result` object that `claude -p --output-format json` prints, or None."""
    text = stdout.strip()
    for candidate in [text] + text.splitlines()[::-1] if text else []:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    return None


def denial(item):
    return {"tool": item.get("tool_name"), "input": tool_input_detail(item.get("tool_input"))}


def validate_report(value, schema=REPORT_SCHEMA, path="report"):
    """Validate the JSON Schema subset used by REPORT_SCHEMA, without dependencies."""
    kind = schema["type"]
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int}[kind]
    if not valid:
        return f"{path} must be {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} has an invalid value"
    if kind == "object":
        properties = schema["properties"]
        if set(schema.get("required", [])) - set(value):
            return f"{path} is missing required fields"
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return f"{path} contains unexpected fields"
        for key, item in value.items():
            error = validate_report(item, properties[key], f"{path}.{key}")
            if error:
                return error
    elif kind == "array":
        for index, item in enumerate(value):
            error = validate_report(item, schema["items"], f"{path}[{index}]")
            if error:
                return error
    return None


def json_lines(stdout):
    """JSONL records, skipping anything else the CLI printed on stdout."""
    for line in stdout.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            yield data


def extract_report(text):
    """The JSON report from a worker's final message: the last ```json block, else the last object."""
    if not isinstance(text, str):
        return None
    for block in reversed(re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)):
        try:
            return json.loads(block)
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    for start in [i for i, char in enumerate(text) if char == "{"][::-1][:200]:
        try:
            value, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def with_stderr_tail(error, stderr):
    tail = stderr.strip()[-1500:]
    return f"{error}: {tail}" if tail else error


def finite_cost(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def record_invocation_cost(record, cost):
    """Pi and OpenCode report what this invocation cost; the session total needs the baseline."""
    claude = record["claude"]
    prior = finite_cost(record.get("cost_baseline_usd"))
    claude["cost_delta_usd"] = round(cost, 10) if cost is not None else None
    total = round(prior + cost, 10) if cost is not None and prior is not None else None
    claude["cost_usd"] = claude["session_cost_usd"] = total


def tool_input_detail(value):
    if not isinstance(value, dict):
        return str(value)[:300]
    detail = value.get("command") or value.get("filePath") or value.get("file_path") or value.get("path") \
        or value.get("url") or json.dumps(value)
    return str(detail)[:300]


def read_pi_events(record, stdout):
    """Fold `pi --mode json` events into the session block. Returns the final text and failure."""
    claude = record["claude"]
    enabled = set(pi_tools(record["mode"]))
    final, failure, cost, turns, models, denials = None, None, None, 0, set(), []
    started = {}  # tool_execution_end does not repeat the arguments; take them from the start event
    for event in json_lines(stdout):
        kind = event.get("type")
        if kind == "tool_execution_start":
            started[event.get("toolCallId")] = event.get("args")
        if kind == "session" and isinstance(event.get("id"), str):
            claude["session_id"] = event["id"]
        elif kind == "turn_end":
            turns += 1
        elif kind == "tool_execution_end" and event.get("isError") and (
                event.get("toolName") not in enabled or PI_GATE_BLOCKED in json.dumps(event.get("result"))):
            # A call outside the profile fails, and the gate blocks commands outside the rules.
            args = event.get("args") or started.get(event.get("toolCallId"))
            denials.append({"tool": event.get("toolName"), "input": tool_input_detail(args)})
        elif kind == "agent_settled" and event.get("aborted"):
            failure = "Pi aborted the run"
        elif kind == "message_end" and isinstance(event.get("message"), dict):
            message = event["message"]
            if message.get("role") != "assistant":
                continue
            if message.get("provider") and message.get("model"):
                models.add(f"{message['provider']}/{message['model']}")
            spent = finite_cost(((message.get("usage") or {}).get("cost") or {}).get("total"))
            if spent is not None:
                cost = (cost or 0) + spent
            reason = message.get("stopReason")
            if reason in ("error", "aborted", "length"):
                failure = message.get("errorMessage") or f"Pi stopped with {reason}"
            elif reason == "stop":
                failure = None
                blocks = message.get("content") if isinstance(message.get("content"), list) else []
                final = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    claude.update(num_turns=turns, models=sorted(models), permission_denials=denials)
    record_invocation_cost(record, cost)
    return final, failure


def read_opencode_events(record, stdout):
    """Fold `opencode run --format json` events into the session block."""
    claude = record["claude"]
    final, failure, cost, turns, denials, errors = None, None, None, 0, [], []
    for line in stdout.splitlines():
        # Without --auto, `opencode run` rejects every permission prompt and says so on stdout.
        match = re.search(r"permission requested: (\S+) \((.*)\); auto-rejecting", line)
        if match:
            denials.append({"tool": match.group(1), "input": match.group(2)[:300]})
    for event in json_lines(stdout):
        if isinstance(event.get("sessionID"), str) and not claude["session_id"]:
            claude["session_id"] = event["sessionID"]
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        kind = event.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            final = part["text"]
        elif kind == "step_finish":
            turns += 1
            spent = finite_cost(part.get("cost"))
            if spent is not None:
                cost = (cost or 0) + spent
        elif kind == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            message = str(state.get("error") or "")
            if state.get("status") == "error" and re.search(r"permission|prevents you|rejected|denied", message, re.I):
                denials.append({"tool": part.get("tool"), "input": tool_input_detail(state.get("input"))})
        elif kind == "error":
            detail = event.get("error") if isinstance(event.get("error"), dict) else {}
            data = detail.get("data") if isinstance(detail.get("data"), dict) else {}
            errors.append(str(data.get("message") or detail.get("name") or "OpenCode reported an error"))
    if errors:
        failure = "; ".join(errors)
    claude.update(num_turns=turns, permission_denials=denials, errors=errors)
    record_invocation_cost(record, cost)
    return final, failure


def read_other_result(record, stdout, stderr, status, error):
    """Pi and OpenCode: the run succeeds only with a clean exit, no reported failure and a valid report."""
    claude, kind = record["claude"], record_worker(record)
    if not any(True for _ in json_lines(stdout)):
        if status == "completed":
            status, error = "error", with_stderr_tail(f"{kind} returned no JSON events", stderr)
        return status, error
    reader = read_pi_events if kind == "pi" else read_opencode_events
    final, failure = reader(record, stdout)
    claude.update(result_text=final, is_error=bool(failure) or claude["exit_code"] not in (0, None),
                  subtype="error" if failure else "success")
    if failure:
        claude["errors"] = claude.get("errors") or [failure]
    claimed = extract_report(final)
    report_error = validate_report(claimed) if claimed is not None else "report is missing"
    record["report_error"] = report_error
    record["claimed"] = claimed if report_error is None else None
    if status == "completed" and (failure or claude["exit_code"] != 0):
        status, error = "error", failure or with_stderr_tail(f"{kind} exited with code {claude['exit_code']}", stderr)
        logged = [line for line in stderr.splitlines() if "level=ERROR" in line and " error=" in line]
        if failure and logged:
            error += " (log: " + logged[-1].split(" error=", 1)[1].split(" cause=", 1)[0][:500] + ")"
    if status == "completed" and report_error:
        status, error = "error", f"{kind} returned an invalid or missing structured report: {report_error}"
    return status, error


def read_result(record, stdout, stderr, status, error):
    if record_worker(record) != "claude":
        return read_other_result(record, stdout, stderr, status, error)
    claude = record["claude"]
    result = parse_result(stdout)
    if result is None:
        if status == "completed":
            status, error = "error", with_stderr_tail("Claude Code returned no JSON result", stderr)
            if re.search(r"unknown option|unexpected option|unrecognized", stderr, re.I):
                error += " (the installed Claude Code may be older than a flag the relay uses; try `claude update`)"
        return status, error
    claude.update(
        session_id=result.get("session_id") or claude["session_id"],
        subtype=result.get("subtype"),
        is_error=bool(result.get("is_error")),
        num_turns=result.get("num_turns"),
        duration_ms=result.get("duration_ms"),
        models=sorted(result.get("modelUsage")) if isinstance(result.get("modelUsage"), dict) else [],
        permission_denials=[denial(item) for item in (result.get("permission_denials") or []) if isinstance(item, dict)]
            if isinstance(result.get("permission_denials", []), list) else [],
        errors=[str(item) for item in (result.get("errors") or [])]
            if isinstance(result.get("errors", []), list) else [],
        result_text=result.get("result") if isinstance(result.get("result"), str) else None,
    )
    cost = finite_cost(result.get("total_cost_usd"))
    claude["cost_usd"] = claude["session_cost_usd"] = cost  # old field retained as an alias
    prior = finite_cost(record.get("cost_baseline_usd"))
    claude["cost_delta_usd"] = round(cost - prior, 10) if cost is not None and prior is not None and cost >= prior else None
    claimed = result.get("structured_output")
    report_error = validate_report(claimed)
    record["report_error"] = report_error
    record["claimed"] = claimed if report_error is None else None
    if status == "completed" and (claude["is_error"] or claude["subtype"] != "success" or claude["exit_code"] != 0):
        status = "error"
        error = (
            "; ".join(claude["errors"])
            or (claude["result_text"] or "")[:500]
            or f"Claude Code ended with {claude['subtype']} (exit code {claude['exit_code']})"
        )
    if status == "completed" and report_error:
        status, error = "error", f"Claude returned an invalid or missing structured report: {report_error}"
    return status, error


def observe(record, root, before):
    """Record what git shows, independent of what Claude reports."""
    observed = record["observed"]
    try:
        if record["mode"] == "review" and root and before:
            after = fingerprint(root)
            paths = set(before["paths"]) | set(after["paths"])
            changed = {p for p in paths if
                       (before["paths"].get(p) is not None and after["paths"].get(p) is not None and
                        before["paths"][p] != after["paths"][p]) or
                       before["index"].get(p) != after["index"].get(p) or
                       before["status"].get(p) != after["status"].get(p)}
            head_changed = before["head"] != after["head"] or before["ref"] != after["ref"]
            observed.update(head_before=before["head"], head_after=after["head"],
                            head_changed=head_changed, coverage_complete=before["complete"] and after["complete"])
            observed["changed_files"] = sorted(changed)
            observed["read_only_violation"] = True if changed or head_changed else (
                False if observed["coverage_complete"] else None)
            if before["head"] and after["head"]:
                observed["commits"] = git(root, "log", "--format=%h %s", f"{before['head']}..{after['head']}").splitlines()
            elif after["head"]:
                observed["commits"] = git(root, "log", "--format=%h %s", after["head"]).splitlines()
        elif record["mode"] == "implement" and record["worktree"]:
            path, base = record["worktree"]["path"], record["worktree"]["base"]
            committed = git(path, "diff", "--name-only", "-z", base, "HEAD").split("\0")
            observed["changed_files"] = sorted(set(git_status(Path(path))) | {p for p in committed if p})
            observed["commits"] = git(path, "log", "--format=%h %s", f"{base}..HEAD").splitlines()
    except Fail as exc:
        observed["error"] = str(exc)


def execute(record):
    """Own a queued reservation, run Claude, and retain it until its group stops."""
    with state_lock():
        record = load(record["run_id"])
        if record["status"] != "queued":
            return record
        if record.get("worker") and record["worker"]["pid"] != os.getpid():
            raise Fail("this run already belongs to another worker")
        record.update(worker_pid=os.getpid(), worker=process_identity(os.getpid()))
        if record["worker"] is None:
            raise Fail("could not establish worker process identity", 1, "error")
        save(record)  # before probing the CLI or reading the repository

    files = record["files"]
    proc, child, job_handle = None, None, None
    job_name = None
    root, before = None, None
    status, error = "completed", None
    handlers = {sig: signal.signal(sig, _interrupt) for sig in STOP_SIGNALS}
    prefix = ""
    try:
        binary, caps = require_worker(record_worker(record))
        record["claude"]["version"] = caps["version"]
        argv, env, prefix = worker_command(record, binary)
        Path(files["run_dir"], "command.json").write_text(json.dumps(argv, indent=2))
        if env:
            # Kept apart from argv; the gated child merges it into its environment.
            Path(files["run_dir"], "command.env.json").write_text(json.dumps(env, indent=2))
        if record["mode"] == "review":
            root = git_root(record["cwd"])
            try:
                before = fingerprint(root) if root else None
            except Fail as exc:
                record["notes"].append(f"Read-only snapshot unavailable: {exc}")
            if before is None:
                record["notes"].append("Read-only tripwire coverage is unavailable; inspect files independently.")
        record.update(status="running", started_at=now(), baseline=before,
                      git_root=str(root) if root else None)
        with state_lock():
            save(record)
        with open(files["stdout"], "wb") as out, open(files["stderr"], "wb") as err:
            # The wrapper cannot exec Claude until we save its process identity
            # and release its stdin gate. A SIGKILL before that closes the pipe.
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS) if os.name != "nt" else None
            try:
                if os.name == "nt":
                    job_handle, job_name = create_windows_job(record["run_id"])
                proc = subprocess.Popen(
                    [python_executable(), str(Path(__file__).resolve()), "_exec", record["run_id"]],
                    cwd=record["cwd"], stdin=subprocess.PIPE, stdout=out, stderr=err,
                    **process_group_kwargs(),
                )
                if job_handle is not None:
                    try:
                        assign_windows_job(job_handle, proc)
                    except OSError:
                        # The gated child has not received permission to start the worker.
                        proc.terminate()
                        proc.wait(timeout=5)
                        raise
                child = process_identity(proc.pid)
                if not child:
                    raise Fail("could not identify the gated Claude process", 1, "error")
                if job_name:
                    child["job_name"] = job_name
                record["child"] = child
                with state_lock():
                    save(record)
            finally:
                if mask is not None:
                    signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            deadline = time.monotonic() + record["request"]["timeout_s"]
            payload = b"G" + prefix.encode() + Path(files["brief"]).read_bytes()
            while True:
                remaining = max(0, deadline - time.monotonic())
                try:
                    proc.communicate(payload, timeout=min(0.5, remaining))
                    break
                except subprocess.TimeoutExpired:
                    payload = None  # communicate() resumes its pending I/O on subsequent calls
                    if load(record["run_id"]).get("cancel_requested"):
                        status, error = "interrupted", "the relay run was cancelled"
                        break
                    if time.monotonic() >= deadline:
                        status, error = "timed_out", f"Claude didn't finish within {record['request']['timeout_s']}s"
                        break
    except Interrupted:
        status, error = "interrupted", "the relay was stopped before Claude finished"
    except (Fail, OSError, ValueError, subprocess.SubprocessError) as exc:
        status, error = getattr(exc, "status", "error"), str(exc)
    finally:
        # Cleanup itself must not be interrupted halfway through the escalation.
        for sig in STOP_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if proc is not None:
                child = child or process_identity(proc.pid)
                stopped = stop_group(child, proc=proc, owned=True, job_handle=job_handle) if child else proc.poll() is not None
                if not stopped:
                    status, error = "orphaned", "execution could not be confirmed stopped; inspect the recorded worker process tree"
                if proc.stdin:
                    proc.stdin.close()
                proc.poll()
                record["claude"]["exit_code"] = proc.returncode
        except (Fail, OSError, subprocess.SubprocessError) as exc:
            status, error = "orphaned", f"could not confirm process cleanup: {exc}"
        finally:
            if job_handle is not None:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job_handle)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)

    stdout = Path(files["stdout"]).read_text(errors="replace") if Path(files["stdout"]).exists() else ""
    stderr = Path(files["stderr"]).read_text(errors="replace") if Path(files["stderr"]).exists() else ""
    status, error = read_result(record, stdout, stderr, status, error)
    observe(record, root, before)
    return finish(record, status, error)


def cmd_exec(args):
    """Internal gated exec: no model starts unless the owner persisted our identity."""
    if os.name != "nt":
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
    if os.read(sys.stdin.fileno(), 1) != b"G":
        return 1
    record = require_run(args.run_id)
    child = record.get("child") or {}
    # The child itself proves its PID cannot have been reused; Windows process
    # start-time precision/access can differ across snapshots for the same live PID.
    if child.get("pid") != os.getpid() or (os.name != "nt" and not same_process(child)):
        raise Fail(f"Claude launch ownership was not recorded (stored PID {child.get('pid')}, current PID {os.getpid()})")
    run_dir = Path(record["files"]["run_dir"])
    argv = json.loads((run_dir / "command.json").read_text())
    overrides = run_dir / "command.env.json"
    env = {**os.environ, **json.loads(overrides.read_text())} if overrides.is_file() else None
    if os.name == "nt":
        # Windows has no execve replacement semantics; remain as a supervised
        # wrapper and forward the gated brief through stdin to the actual CLI.
        worker = subprocess.Popen(command_for(argv[0], argv[1:]), stdin=sys.stdin, env=env,
                                  **process_group_kwargs())
        return worker.wait()
    if env is not None:
        os.execve(argv[0], argv, env)
    os.execv(argv[0], argv)


# --- commands --------------------------------------------------------------------


def other_auth(kind, binary, model, notes):
    """Pi can check one provider's credentials; OpenCode has no machine-readable sign-in check."""
    if kind == "opencode":
        notes.append("OpenCode has no machine-readable sign-in check; run `opencode auth list` to see providers.")
        return None
    if not model:
        notes.append("Pi checks credentials per provider; pass --model to check sign-in.")
        return None
    try:
        proc = subprocess.run(command_for(binary, ["auth", "check", "--model", model, "--json", "--no-refresh"]),
                              capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout)
        # Only the readiness fields: never echo a credential back.
        return {"loggedIn": proc.returncode == 0 and data.get("status") == "ready",
                "status": data.get("status"), "provider": data.get("provider"), "authType": data.get("authType")}
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        notes.append("Could not read pi auth status.")
        return None


def host_status():
    """Checks shared by every worker: git, state folder and process inspection."""
    report = {"git": shutil.which("git"), "python": platform.python_version(),
              "state_dir": str(crew_home()), "state_writable": False, "process_inspection": False, "notes": []}
    try:
        crew_home().mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryFile(dir=crew_home()) as probe:
            probe.write(b"crew")
        report["state_writable"] = True
    except OSError:
        report["notes"].append("The state directory is not writable.")
    try:
        report["process_inspection"] = process_identity(os.getpid()) is not None
    except (OSError, Fail, subprocess.SubprocessError):
        report["notes"].append("Native process inspection is required for safe cancellation.")
    if not report["git"]:
        report["notes"].append("git is required for worktrees and the read-only tripwire.")
    report["ok"] = bool(report["git"] and report["state_writable"] and report["process_inspection"])
    return report


def worker_status(kind, model=None):
    """One worker's CLI, version, profile flags and sign-in, without a model call.

    `sign_in` is confirmed, not_confirmed, or unchecked (OpenCode, and Pi without
    a model, have no sign-in check). Only not_confirmed makes a worker not ready.
    """
    spec = WORKER_SPECS[kind]
    binary = worker_bin(kind)
    status = {"worker": kind, "found": bool(binary), "binary": binary, "version": None,
              "capabilities": None, "auth": None, "sign_in": "unchecked", "ready": False, "notes": []}
    if not binary:
        status["notes"].append(f"{kind} not found. {spec['install']}, or set {spec['bin_env']}.")
        return status
    try:
        command_for(binary, ["--version"])
    except Fail as exc:
        status["notes"].append(str(exc))
        return status
    caps = status["capabilities"] = capabilities(binary, kind)
    status["version"] = caps["version"]
    if not caps["restricted_profile"]:
        status["notes"].append(f"{kind} {caps['minimum_version']}+ with the relay's profile flags is required. "
                               "Update it; no weaker fallback is used.")
    if kind == "claude":
        try:
            proc = subprocess.run(command_for(binary, ["auth", "status", "--json"]),
                                  capture_output=True, text=True, timeout=30)
            data = json.loads(proc.stdout)
            status["auth"] = {key: data.get(key) for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
            if proc.returncode:
                status["auth"]["loggedIn"] = False
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
            status["notes"].append("Could not read claude auth status.")
            status["auth"] = {"loggedIn": None}
    else:
        status["auth"] = other_auth(kind, binary, model, status["notes"])
    if status["auth"] is not None:
        status["sign_in"] = "confirmed" if status["auth"].get("loggedIn") is True else "not_confirmed"
    if status["sign_in"] == "not_confirmed":
        status["notes"].append(f"{kind} did not confirm authentication. {spec['sign_in']} Check credentials and "
                               "network/keychain access under the host's permission policy.")
    status["ready"] = caps["restricted_profile"] and status["sign_in"] != "not_confirmed"
    return status


def cmd_preflight(args):
    kind = args.worker
    host, status = host_status(), worker_status(kind, args.model)
    report = {
        "ok": False, "worker": kind, "binary": status["binary"], "version": status["version"], "auth": status["auth"],
        "git": host["git"], "python": host["python"], "state_dir": host["state_dir"],
        "state_writable": host["state_writable"], "capabilities": status["capabilities"],
        "process_inspection": host["process_inspection"], "notes": host["notes"] + status["notes"],
    }
    if kind == "claude":
        report["claude"] = status["binary"]  # earlier name of `binary`, kept for existing callers
    if not status["found"]:
        return emit(report, 127)
    report["ok"] = bool(host["ok"] and status["ready"])
    return emit(report, 0 if report["ok"] else 1)


def cmd_workers(args):
    """Which workers this machine can run, for setup and for choosing a worker."""
    host = host_status()
    # Each check spawns two or three CLI processes; run the workers side by side.
    with ThreadPoolExecutor(max_workers=len(WORKERS)) as pool:
        workers = list(pool.map(lambda kind: worker_status(kind, args.pi_model if kind == "pi" else None), WORKERS))
    for status in workers:
        spec = WORKER_SPECS[status["worker"]]
        if not status["found"]:
            status["install"] = spec["install"]
        elif status["sign_in"] != "confirmed":
            status["sign_in_hint"] = spec["sign_in"]
    available = [status["worker"] for status in workers if status["ready"]] if host["ok"] else []
    report = {"ok": bool(available), "host": host, "workers": workers, "available": available,
              "default": "claude" if "claude" in available else (available[0] if available else None)}
    return emit(report, 0 if available else 1)


def resolve_dir(value):
    path = Path(value or os.getcwd()).expanduser().resolve()
    if not path.is_dir():
        raise Fail(f"{path} is not a folder")
    return path


def resolve_resume(args):
    """Where and how to continue an earlier session: (mode, cwd, worktree, resume_from)."""
    if args.worktree or args.base:
        raise Fail("--worktree and --base can't be combined with --resume; the run continues where the session started")
    previous = find_run(args.resume)
    if previous:
        if previous["status"] in ACTIVE:
            raise Fail(f"run {previous['run_id']} is still active; inspect status or cancel it first")
        if args.mode and args.mode != previous["mode"]:
            raise Fail(f"run {previous['run_id']} is a {previous['mode']} run; start a new run to switch modes")
        session_id = previous["claude"]["session_id"]
        if not session_id:
            raise Fail(f"run {previous['run_id']} has no worker session id to resume")
        if not Path(previous["cwd"]).is_dir():
            raise Fail(f"{previous['cwd']} no longer exists, so its session can't be resumed there")
        resume_from = {"run_id": previous["run_id"], "session_id": session_id}
        return previous["mode"], Path(previous["cwd"]), previous["worktree"], resume_from
    if not SESSION_RE.fullmatch(args.resume):
        raise Fail(f"no run or session found for {args.resume!r}")
    if args.mode != "review":
        raise Fail("to resume a session the relay didn't start, pass --mode review and the --cwd it ran in")
    return "review", resolve_dir(args.cwd), None, {"run_id": None, "session_id": args.resume}


def check_allow_rules(rules):
    for rule in rules:
        match = RULE_RE.fullmatch(rule)
        if not match or match.group(1) not in EXTRA_TOOLS or any(c in rule for c in (",", "\n", "\r")):
            raise Fail(f"--allow takes scoped Bash rules, WebFetch or WebSearch, not {rule!r}")
        if match.group(1) == "Bash":
            command = match.group(2)[1:-1].strip() if match.group(2) else ""
            # Require a literal executable prefix. Do not accept wildcard-only,
            # legacy ':*', comma-separated rule injection, or multiline rules.
            if not re.match(r"[A-Za-z0-9_./-]+(?:\s|$)", command):
                raise Fail(f"--allow {rule!r} needs a literal command prefix, e.g. Bash(pytest *)")
    return list(dict.fromkeys(rules))


def effective_request(args, previous):
    inherited = {**DEFAULT_REQUEST, **((previous or {}).get("request") or {})}
    overrides = {}
    for field, argument in (("model", "model"), ("effort", "effort"),
                            ("max_turns", "max_turns"), ("max_budget_usd", "max_budget_usd"),
                            ("timeout_s", "timeout")):
        value = getattr(args, argument)
        if value is not None:
            inherited[field] = overrides[field] = value
    allow = [] if args.clear_allow else list(inherited["allow"])
    if args.allow:
        allow.extend(args.allow)
    inherited["allow"] = check_allow_rules(allow)
    if args.allow is not None or args.clear_allow:
        overrides["allow"] = inherited["allow"]
    request = {key: inherited[key] for key in DEFAULT_REQUEST}
    if request["model"] and not MODEL_RE.fullmatch(request["model"]):
        raise Fail(f"--model {request['model']!r} doesn't look like a model alias or name")
    if request["max_turns"] < 1 or request["timeout_s"] < 1:
        raise Fail("--max-turns and --timeout must be at least 1")
    budget = request["max_budget_usd"]
    if budget is not None and (not math.isfinite(budget) or budget <= 0):
        raise Fail("--max-budget-usd must be a finite number greater than 0")
    request["overrides"] = overrides
    return request


def check_worker_request(kind, request, args):
    """Refuse limits a worker cannot enforce, rather than record them as if they held."""
    limits, notes = WORKER_SPECS[kind]["limits"], []
    if not limits["budget"] and request["max_budget_usd"] is not None:
        raise Fail(f"{kind} has no spending limit, so --max-budget-usd can't be enforced; bound the run with --timeout")
    if not limits["turns"]:
        if args.max_turns is not None:
            raise Fail(f"{kind} has no turn limit, so --max-turns can't be enforced; bound the run with --timeout")
        notes.append(f"{kind} has no turn limit; only --timeout bounds this run.")
    if not limits["web"] and any(not rule.startswith("Bash") for rule in request["allow"]):
        raise Fail(f"{kind}'s profile has no web tools; WebFetch and WebSearch can't be allowed")
    return notes


def assert_available(mode, cwd, session_id):
    """Run under state_lock so checking ownership and reserving it are atomic."""
    workspace = git_root(cwd) or cwd.resolve()
    for active in all_records():
        if active["status"] not in ACTIVE:
            continue
        source_session = (active["request"].get("resume_from") or {}).get("session_id")
        same_session = session_id and session_id in (active["claude"].get("session_id"), source_session)
        active_workspace = (Path(active["workspace"]) if active.get("workspace") else
                            git_root(active["cwd"]) or Path(active["cwd"]).resolve())
        same_folder = active_workspace == workspace
        if same_session or (same_folder and "implement" in (mode, active["mode"])):
            raise Fail(f"run {active['run_id']} owns this session or worktree; inspect status or cancel it before continuing")


def cmd_run(args):
    if args.fork and not args.resume:
        raise Fail("--fork needs --resume")
    try:
        brief = sys.stdin.read() if args.brief == "-" else Path(args.brief).expanduser().read_text()
    except OSError as exc:
        raise Fail(f"could not read the brief: {exc}")
    if not brief.strip():
        raise Fail("the brief is empty")
    previous = find_run(args.resume) if args.resume else None
    kind = record_worker(previous) if previous else (args.worker or "claude")
    if previous and args.worker and args.worker != kind:
        raise Fail(f"run {previous['run_id']} used the {kind} worker; a session can't move to another worker")
    request = effective_request(args, previous)
    if args.resume:
        mode, cwd, worktree, resume_from = resolve_resume(args)
        session_id = resume_from["session_id"]
        if args.fork:
            session_id = str(uuid.uuid4()) if "fork" in WORKER_SPECS[kind]["preset_session"] else None
        if args.cwd and resolve_dir(args.cwd) != cwd:
            raise Fail("--cwd differs from the recorded session directory")
    else:
        if not args.mode:
            raise Fail("--mode is required: review or implement")
        mode, worktree, resume_from = args.mode, None, None
        session_id = str(uuid.uuid4()) if "new" in WORKER_SPECS[kind]["preset_session"] else None
        if mode == "implement" and not args.worktree:
            raise Fail("implement runs need --worktree NAME")
        if mode == "review" and (args.worktree or args.base):
            raise Fail("--worktree and --base only apply to implement runs")
        cwd = resolve_dir(args.cwd)
    worker_notes = check_worker_request(kind, request, args)
    _, caps = require_worker(kind)
    request.update(resume_from=resume_from, fork=args.fork, background=args.background)
    notes = []
    # Also reserve review access against an active implementation in that checkout.
    # Independent new worktrees do not conflict with work in the source checkout.
    with state_lock():
        if args.resume or mode == "review":
            assert_available(mode, cwd, (resume_from or {}).get("session_id"))
        if mode == "implement" and not args.resume:
            worktree, notes = create_worktree(cwd, args.worktree, args.base or "HEAD")
            cwd = Path(worktree["path"])
        notes = notes + worker_notes
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        run_dir = runs_dir() / run_id
        run_dir.mkdir(parents=True, mode=0o700)
        (run_dir / "brief.md").write_text(brief)
        prior_cost = 0 if not args.resume else None
        if previous and not args.fork and previous["status"] == "completed":
            latest = find_run(previous["claude"]["session_id"])
            if latest and latest["run_id"] == previous["run_id"]:
                prior_cost = previous["claude"].get("session_cost_usd", previous["claude"].get("cost_usd"))
        record = {
            "schema": RECORD_SCHEMA, "run_id": run_id, "status": "queued", "agent": kind,
            "mode": mode, "cwd": str(cwd), "workspace": str(git_root(cwd) or cwd), "worktree": worktree,
            "created_at": now(), "started_at": None, "finished_at": None,
            "codex_thread_id": os.environ.get("CODEX_THREAD_ID"), "request": request,
            # The worker session block. It is named `claude` for every worker so
            # existing readers of schema v1 records keep working.
            "claude": {
                "version": caps["version"], "session_id": session_id, "exit_code": None,
                "subtype": None, "is_error": None, "num_turns": None, "duration_ms": None,
                "cost_usd": None, "session_cost_usd": None, "cost_delta_usd": None,
                "models": [], "permission_denials": [], "errors": [], "result_text": None, "transcript": None,
            },
            "cost_baseline_usd": prior_cost, "claimed": None, "report_error": None,
            "observed": {"changed_files": [], "commits": [], "read_only_violation": None},
            "verification": {"verdict": "pending", "checks": [], "notes": None, "at": None},
            "notes": notes, "error": None, "worker_pid": None, "worker": None,
            "launcher": process_identity(os.getpid()), "child": None, "gated_launch": True,
            "files": {"run_dir": str(run_dir), "brief": str(run_dir / "brief.md"),
                      "stdout": str(run_dir / WORKER_SPECS[kind]["stdout"]),
                      "stderr": str(run_dir / f"{kind}.stderr.log")},
        }
        save(record)
        if args.background:
            try:
                with open(run_dir / "worker.log", "ab") as log:
                    worker = subprocess.Popen(
                        [python_executable(), str(Path(__file__).resolve()), "_worker", run_id],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, **process_group_kwargs(detached=True),
                    )
                record.update(worker_pid=worker.pid, worker=process_identity(worker.pid))
                save(record)  # worker cannot adopt the record until we release the lock
            except OSError as exc:
                record.update(status="error", error=f"could not start worker: {exc}", finished_at=now())
                save(record)
            return emit(record, 1 if record["status"] == "error" else 0)
    record = execute(record)
    return emit(record, {"completed": 0, "claude_unavailable": 127, "worker_unavailable": 127}.get(record["status"], 1))


def cmd_worker(args):
    record = execute(require_run(args.run_id))
    return 0 if record["status"] == "completed" else 1


def cmd_status(args):
    record = require_run(args.run)
    deadline = time.monotonic() + max(args.wait, 0)
    while True:
        record = refresh(record)
        if record["status"] not in ACTIVE or time.monotonic() >= deadline:
            return emit(record)
        time.sleep(2)


def cmd_list(args):
    thread = os.environ.get("CODEX_THREAD_ID")
    if args.thread and not thread:
        raise Fail("CODEX_THREAD_ID isn't set, so there's no thread to filter by")
    rows = []
    for record in all_records():
        if args.thread and record.get("codex_thread_id") != thread:
            continue
        record = refresh(record)
        rows.append({
            "run_id": record["run_id"],
            "created_at": record["created_at"],
            "worker": record_worker(record),
            "mode": record["mode"],
            "status": record["status"],
            "cwd": record["cwd"],
            "session_id": record["claude"]["session_id"],
            "claimed": (record["claimed"] or {}).get("status"),
            "verdict": record["verification"]["verdict"],
        })
        if len(rows) >= args.limit:
            break
    return emit(rows)


def cmd_verify(args):
    record = refresh(require_run(args.run))
    if record["status"] in ACTIVE:
        raise Fail("the run is still in progress")
    checks = []
    for item in args.check:
        result, sep, command = item.partition("=")
        if not sep or result not in ("passed", "failed") or not command.strip():
            raise Fail(f"--check {item!r} should look like 'passed=npm test' or 'failed=npm test'")
        checks.append({"command": command.strip(), "status": result})
    with state_lock():
        record = load(record["run_id"])
        if record["status"] in ACTIVE:
            raise Fail("the run is still in progress")
        assert_available(record["mode"], Path(record["cwd"]), record["claude"]["session_id"])
        record["verification"] = {"verdict": args.verdict, "checks": checks, "notes": args.note, "at": now()}
        save(record)
    return emit(record)


def cmd_cancel(args):
    record = refresh(require_run(args.run))
    if record["status"] not in ACTIVE:
        return emit(record)
    with state_lock():
        current = load(record["run_id"])
        if current["status"] in ACTIVE:
            current["cancel_requested"] = True
            save(current)
    if os.name != "nt" and same_process(record.get("worker")):
        try:
            os.kill(record["worker"]["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        record = refresh(record)
        if record["status"] not in ACTIVE or record["status"] == "orphaned":
            break
        time.sleep(1)
    return emit(record, 1 if record["status"] in ACTIVE else 0)


def build_parser():
    parser = argparse.ArgumentParser(prog="relay.py", description="Run bounded tasks in a local Claude Code session for Codex.")
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="check a worker CLI, its version and sign-in without a model call")
    preflight.add_argument("--worker", choices=WORKERS, default="claude")
    preflight.add_argument("--model", help="pi: the model whose provider credentials to check")

    workers = sub.add_parser("workers", help="check every worker CLI (claude, pi, opencode) without a model call")
    workers.add_argument("--pi-model", help="the model whose provider credentials Pi should check")

    run = sub.add_parser("run", help="start a review or implement run")
    run.add_argument("--worker", choices=WORKERS, help="claude (default), pi or opencode; resumes keep their worker")
    run.add_argument("--mode", choices=("review", "implement"))
    run.add_argument("--brief", required=True, help="path to the brief, or - to read it from stdin")
    run.add_argument("--cwd", help="folder to work in (default: the current folder)")
    run.add_argument("--worktree", metavar="NAME", help="implement: create a git worktree on branch crew/NAME")
    run.add_argument("--base", metavar="REF", help="implement: commit the worktree starts from (default: HEAD)")
    run.add_argument("--model", help="model alias or name, e.g. sonnet (claude), anthropic/claude-sonnet-5-5 (pi, opencode)")
    run.add_argument("--effort", choices=EFFORTS, help="claude --effort, pi --thinking, opencode --variant")
    run.add_argument("--allow", action="append", default=None, metavar="RULE",
                     help="extra permission rule, e.g. 'Bash(npm test *)' (repeatable)")
    run.add_argument("--clear-allow", action="store_true",
                     help="discard inherited --allow rules before adding new ones")
    run.add_argument("--max-turns", type=int, default=None)
    run.add_argument("--max-budget-usd", type=float)
    run.add_argument("--timeout", type=int, default=None, help="seconds before the relay stops Claude (default: 1800; inherited on resume)")
    run.add_argument("--resume", metavar="RUN_OR_SESSION", help="send the brief as a follow-up in an earlier run's Claude session")
    run.add_argument("--fork", action="store_true", help="with --resume: branch the session instead of continuing it")
    run.add_argument("--background", action="store_true", help="return at once and poll with `status RUN --wait SECONDS`")

    status = sub.add_parser("status", help="print a run record")
    status.add_argument("run", help="run id or Claude session id")
    status.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="wait up to SECONDS for the run to finish")

    listing = sub.add_parser("list", help="list recent runs, newest first")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--thread", action="store_true", help="only runs started from this Codex thread")

    verify = sub.add_parser("verify", help="record Codex's verification, kept apart from Claude's claim")
    verify.add_argument("run", help="run id or Claude session id")
    verify.add_argument("--verdict", required=True, choices=VERDICTS)
    verify.add_argument("--check", action="append", default=[], metavar="RESULT=COMMAND",
                        help="a check Codex ran, e.g. 'passed=npm test' (repeatable)")
    verify.add_argument("--note", help="one line on what was confirmed or what's wrong")

    cancel = sub.add_parser("cancel", help="stop a running run")
    cancel.add_argument("run", help="run id or Claude session id")

    worker = sub.add_parser("_worker")  # internal: runs a --background run
    worker.add_argument("run_id")
    child = sub.add_parser("_exec")  # internal gated exec, before any model can run
    child.add_argument("run_id")
    return parser


COMMANDS = {
    "preflight": cmd_preflight,
    "workers": cmd_workers,
    "run": cmd_run,
    "status": cmd_status,
    "list": cmd_list,
    "verify": cmd_verify,
    "cancel": cmd_cancel,
    "_worker": cmd_worker,
    "_exec": cmd_exec,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except Fail as exc:
        return emit({"status": exc.status, "error": str(exc)}, exc.code)
    except (OSError, subprocess.SubprocessError) as exc:
        return emit({"status": "error", "error": str(exc)}, 1)


if __name__ == "__main__":
    sys.exit(main())
