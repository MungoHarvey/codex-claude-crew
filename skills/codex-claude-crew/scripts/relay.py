#!/usr/bin/env python3
"""Run one bounded task in a local Claude Code session for Codex.

Codex calls this script instead of building `claude -p` commands by hand. It sets
the permission boundary for the chosen mode, creates the git worktree for
implement runs, captures Claude's JSON output, and writes a run record that keeps
Claude's claims apart from Codex's own verification.

Standard library only. macOS and Linux.

  relay.py preflight                    check the claude CLI and sign-in (no model call)
  relay.py run --mode review ...        start a run (see `relay.py run --help`)
  relay.py status RUN [--wait SECONDS]  print a run record
  relay.py list [--limit N] [--thread]  list recent runs
  relay.py verify RUN --verdict V ...   record Codex's own verification
  relay.py cancel RUN                   stop a running run

Environment: CLAUDE_CREW_HOME (state folder, default ~/.codex-claude-crew) and
CLAUDE_CREW_BIN (path to the claude executable, default: found on PATH).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

RECORD_SCHEMA = "codex-claude-crew.run/v1"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
VERDICTS = ("confirmed", "partial", "rejected")
ACTIVE = ("queued", "running")
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
# First Claude Code release with `--permission-prompts none`.
PERMISSION_PROMPTS_SINCE = (2, 1, 259)

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
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
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
You are a worker session started by Codex, another coding agent, through codex-claude-crew. \
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
    return Path(os.environ.get("CLAUDE_CREW_HOME") or Path.home() / ".codex-claude-crew")


def runs_dir():
    return crew_home() / "runs"


# --- Claude Code CLI ---------------------------------------------------------


def claude_bin():
    configured = os.environ.get("CLAUDE_CREW_BIN")
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    return shutil.which("claude")


def claude_version(binary):
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\d+\.\d+\.\d+", proc.stdout)
    return match.group(0) if match else None


def version_tuple(version):
    return tuple(int(part) for part in version.split(".")) if version else ()


def claude_args(record, version):
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
        "--allowedTools", ",".join(allowed + request["allow"]),
        "--disallowedTools", ",".join(DENY_RULES),
        "--strict-mcp-config",  # with no --mcp-config: no MCP servers
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
    if version_tuple(version) >= PERMISSION_PROMPTS_SINCE:
        args += ["--permission-prompts", "none"]
    return args


def transcript_path(cwd, session_id):
    """Claude Code keeps each session at ~/.claude/projects/<cwd with non-alphanumerics as ->/<id>.jsonl."""
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
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
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
    """Working-tree snapshot for the read-only tripwire. Sees git-visible changes only."""
    status = git_status(root)
    digest = hashlib.sha256(git(root, "diff", "HEAD", "--binary", check=False).encode("utf-8", "replace"))
    for path, code in sorted(status.items()):
        if code == "??":
            try:
                digest.update(f"{path}:{os.stat(root / path).st_mtime_ns}".encode())
            except OSError:
                pass
    return status, digest.hexdigest()


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


def record_path(run_id):
    return runs_dir() / run_id / "run.json"


def save(record):
    path = record_path(record["run_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    os.replace(tmp, path)


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
    """Look a run up by run id, or by Claude session id (newest run of that session)."""
    if RUN_ID_RE.fullmatch(ref) and record_path(ref).is_file():
        return load(ref)
    if UUID_RE.fullmatch(ref):
        for record in all_records():
            if record["claude"].get("session_id") == ref:
                return record
    return None


def require_run(ref):
    record = find_run(ref)
    if not record:
        raise Fail(f"no run found for {ref!r}")
    return record


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def refresh(record):
    """Reload a record, and close it out if the process running it died without finishing."""
    record = load(record["run_id"])
    pid = record.get("worker_pid")
    if record["status"] in ACTIVE and pid and not alive(pid):
        record = load(record["run_id"])  # it may have finished between the two reads
        if record["status"] in ACTIVE:
            return finish(record, "error", "the relay stopped before the run finished; see claude.stderr.log, or resume the session")
    return record


def finish(record, status, error):
    session_id = record["claude"]["session_id"]
    if session_id:
        record["claude"]["transcript"] = transcript_path(record["cwd"], session_id)
    record.update(status=status, error=error, finished_at=now())
    save(record)
    return record


# --- running Claude -----------------------------------------------------------------


def _interrupt(signum, frame):
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise Interrupted()


def stop(proc):
    """Ask Claude to end its turn (SIGINT), then escalate. Signals go to Claude's whole process group."""
    for sig, grace in ((signal.SIGINT, 15), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


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
    tool_input = item.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    detail = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("url") or json.dumps(tool_input)
    return {"tool": item.get("tool_name"), "input": str(detail)[:300]}


def read_result(record, stdout, stderr, status, error):
    claude = record["claude"]
    result = parse_result(stdout)
    if result is None:
        if status == "completed":
            status, error = "error", "Claude Code returned no JSON result"
            tail = stderr.strip()[-1500:]
            if tail:
                error += f": {tail}"
            if re.search(r"unknown option|unexpected option|unrecognized", stderr, re.I):
                error += " (the installed Claude Code may be older than a flag the relay uses; try `claude update`)"
        return status, error
    claude.update(
        session_id=result.get("session_id") or claude["session_id"],
        subtype=result.get("subtype"),
        is_error=bool(result.get("is_error")),
        num_turns=result.get("num_turns"),
        duration_ms=result.get("duration_ms"),
        cost_usd=result.get("total_cost_usd"),
        models=sorted(result.get("modelUsage") or {}),
        permission_denials=[denial(item) for item in result.get("permission_denials") or []],
        errors=[str(item) for item in result.get("errors") or []],
        result_text=result.get("result"),
    )
    claimed = result.get("structured_output")
    record["claimed"] = claimed if isinstance(claimed, dict) else None
    if status == "completed" and (claude["is_error"] or claude["subtype"] != "success" or claude["exit_code"] != 0):
        status = "error"
        error = (
            "; ".join(claude["errors"])
            or (claude["result_text"] or "")[:500]
            or f"Claude Code ended with {claude['subtype']} (exit code {claude['exit_code']})"
        )
    return status, error


def observe(record, root, before):
    """Record what git shows, independent of what Claude reports."""
    observed = record["observed"]
    try:
        if record["mode"] == "review" and root and before:
            after = fingerprint(root)
            observed["read_only_violation"] = after != before
            paths = set(before[0]) | set(after[0])
            observed["changed_files"] = sorted(p for p in paths if before[0].get(p) != after[0].get(p))
        elif record["mode"] == "implement" and record["worktree"]:
            path, base = record["worktree"]["path"], record["worktree"]["base"]
            committed = git(path, "diff", "--name-only", "-z", base, "HEAD").split("\0")
            observed["changed_files"] = sorted(set(git_status(Path(path))) | {p for p in committed if p})
            observed["commits"] = git(path, "log", "--format=%h %s", f"{base}..HEAD").splitlines()
    except Fail as exc:
        observed["error"] = str(exc)


def execute(record):
    """Run Claude for a saved record and fill in the result. Used by foreground runs and _worker."""
    record["worker_pid"] = os.getpid()
    files = record["files"]
    binary = claude_bin()
    if not binary:
        return finish(record, "claude_unavailable", "claude not found; install Claude Code or set CLAUDE_CREW_BIN")
    version = claude_version(binary)
    record["claude"]["version"] = version
    argv = [binary, *claude_args(record, version)]
    Path(files["run_dir"], "command.json").write_text(json.dumps(argv, indent=2))

    root, before = None, None
    if record["mode"] == "review":
        root = git_root(record["cwd"])
        try:
            before = fingerprint(root) if root else None
        except Fail:
            root = None
    record.update(status="running", started_at=now())
    save(record)

    status, error = "completed", None
    timeout = record["request"]["timeout_s"]
    with open(files["stdout"], "wb") as out, open(files["stderr"], "wb") as err:
        try:
            proc = subprocess.Popen(
                argv, cwd=record["cwd"], stdin=subprocess.PIPE, stdout=out, stderr=err,
                start_new_session=True,  # own process group, so stop() reaches Claude's children too
            )
        except OSError as exc:
            return finish(record, "claude_unavailable", f"could not start claude: {exc}")
        handlers = {sig: signal.signal(sig, _interrupt) for sig in STOP_SIGNALS}
        try:
            try:
                proc.communicate(Path(files["brief"]).read_bytes(), timeout=timeout)
            except subprocess.TimeoutExpired:
                status, error = "timed_out", f"Claude didn't finish within {timeout}s"
                stop(proc)
        except Interrupted:
            status, error = "interrupted", "the relay was stopped before Claude finished"
            stop(proc)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)

    record["claude"]["exit_code"] = proc.returncode
    stdout = Path(files["stdout"]).read_text(errors="replace")
    stderr = Path(files["stderr"]).read_text(errors="replace")
    status, error = read_result(record, stdout, stderr, status, error)
    observe(record, root, before)
    return finish(record, status, error)


# --- commands --------------------------------------------------------------------


def cmd_preflight(args):
    binary = claude_bin()
    report = {
        "ok": False,
        "claude": binary,
        "version": None,
        "auth": None,
        "git": shutil.which("git"),
        "python": platform.python_version(),
        "state_dir": str(crew_home()),
        "notes": [],
    }
    if not binary:
        report["notes"].append("claude not found. Install Claude Code and sign in, or set CLAUDE_CREW_BIN.")
        return emit(report, 127)
    report["version"] = claude_version(binary)
    try:
        proc = subprocess.run([binary, "auth", "status", "--json"], capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout)
        # Keep only these keys: the full output can include the account email.
        report["auth"] = {key: data.get(key) for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        report["notes"].append("Could not read `claude auth status`.")
    if not report["git"]:
        report["notes"].append("git not found; implement runs and the read-only check need it.")
    logged_in = (report["auth"] or {}).get("loggedIn")
    if logged_in is not True:
        report["notes"].append(
            "Claude Code doesn't report a sign-in. Inside the Codex sandbox this can be a false alarm: "
            "rerun preflight outside it before asking the user to run `claude` and sign in."
        )
    report["ok"] = bool(report["version"]) and logged_in is True
    return emit(report, 0 if report["ok"] else 1)


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
        if args.mode and args.mode != previous["mode"]:
            raise Fail(f"run {previous['run_id']} is a {previous['mode']} run; start a new run to switch modes")
        session_id = previous["claude"]["session_id"]
        if not session_id:
            raise Fail(f"run {previous['run_id']} has no Claude session id to resume")
        if not Path(previous["cwd"]).is_dir():
            raise Fail(f"{previous['cwd']} no longer exists, so its session can't be resumed there")
        resume_from = {"run_id": previous["run_id"], "session_id": session_id}
        return previous["mode"], Path(previous["cwd"]), previous["worktree"], resume_from
    if not UUID_RE.fullmatch(args.resume):
        raise Fail(f"no run or session found for {args.resume!r}")
    if args.mode != "review":
        raise Fail("to resume a session the relay didn't start, pass --mode review and the --cwd it ran in")
    return "review", resolve_dir(args.cwd), None, {"run_id": None, "session_id": args.resume}


def check_allow_rules(rules):
    for rule in rules:
        match = RULE_RE.fullmatch(rule)
        if not match or match.group(1) not in EXTRA_TOOLS:
            raise Fail(f"--allow takes 'Bash(...)' rules, WebFetch or WebSearch, not {rule!r}")
        if match.group(1) == "Bash" and not match.group(2):
            raise Fail("--allow Bash would allow every command; use a scoped rule like 'Bash(npm test *)'")
    return list(rules)


def cmd_run(args):
    if args.model and not MODEL_RE.fullmatch(args.model):
        raise Fail(f"--model {args.model!r} doesn't look like a model alias or name")
    if args.max_turns < 1 or args.timeout < 1:
        raise Fail("--max-turns and --timeout must be at least 1")
    if args.max_budget_usd is not None and args.max_budget_usd <= 0:
        raise Fail("--max-budget-usd must be more than 0")
    if args.fork and not args.resume:
        raise Fail("--fork needs --resume")
    try:
        brief = sys.stdin.read() if args.brief == "-" else Path(args.brief).expanduser().read_text()
    except OSError as exc:
        raise Fail(f"could not read the brief: {exc}")
    if not brief.strip():
        raise Fail("the brief is empty")

    notes = []
    if args.resume:
        mode, cwd, worktree, resume_from = resolve_resume(args)
        session_id = None if args.fork else resume_from["session_id"]
    else:
        if not args.mode:
            raise Fail("--mode is required: review or implement")
        mode, worktree, resume_from, session_id = args.mode, None, None, str(uuid.uuid4())
        if mode == "implement" and not args.worktree:
            raise Fail("implement runs need --worktree NAME: Claude works in a new git worktree on branch crew/NAME")
        if mode == "review" and (args.worktree or args.base):
            raise Fail("--worktree and --base only apply to implement runs")
        cwd = resolve_dir(args.cwd)
    allow = check_allow_rules(args.allow)
    if not claude_bin():
        raise Fail("claude not found. Install Claude Code and sign in, or set CLAUDE_CREW_BIN.", 127, "claude_unavailable")
    if mode == "implement" and not args.resume:
        worktree, notes = create_worktree(cwd, args.worktree, args.base or "HEAD")
        cwd = Path(worktree["path"])

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    run_dir = runs_dir() / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "brief.md").write_text(brief)
    record = {
        "schema": RECORD_SCHEMA,
        "run_id": run_id,
        "status": "queued",
        "mode": mode,
        "cwd": str(cwd),
        "worktree": worktree,
        "created_at": now(),
        "started_at": None,
        "finished_at": None,
        "codex_thread_id": os.environ.get("CODEX_THREAD_ID"),
        "request": {
            "model": args.model,
            "effort": args.effort,
            "max_turns": args.max_turns,
            "max_budget_usd": args.max_budget_usd,
            "timeout_s": args.timeout,
            "allow": allow,
            "resume_from": resume_from,
            "fork": args.fork,
            "background": args.background,
        },
        "claude": {
            "version": None,
            "session_id": session_id,
            "exit_code": None,
            "subtype": None,
            "is_error": None,
            "num_turns": None,
            "duration_ms": None,
            "cost_usd": None,
            "models": [],
            "permission_denials": [],
            "errors": [],
            "result_text": None,
            "transcript": None,
        },
        "claimed": None,
        "observed": {"changed_files": [], "commits": [], "read_only_violation": None},
        "verification": {"verdict": "pending", "checks": [], "notes": None, "at": None},
        "notes": notes,
        "error": None,
        "worker_pid": None,
        "files": {
            "run_dir": str(run_dir),
            "brief": str(run_dir / "brief.md"),
            "stdout": str(run_dir / "claude.stdout.json"),
            "stderr": str(run_dir / "claude.stderr.log"),
        },
    }
    save(record)

    if args.background:
        with open(run_dir / "worker.log", "ab") as log:
            worker = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "_worker", run_id],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        record["worker_pid"] = worker.pid
        return emit(record)
    record = execute(record)
    return emit(record, {"completed": 0, "claude_unavailable": 127}.get(record["status"], 1))


def cmd_worker(args):
    execute(load(args.run_id))
    return 0


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
        rows.append({
            "run_id": record["run_id"],
            "created_at": record["created_at"],
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
    record["verification"] = {"verdict": args.verdict, "checks": checks, "notes": args.note, "at": now()}
    save(record)
    return emit(record)


def cmd_cancel(args):
    record = refresh(require_run(args.run))
    if record["status"] not in ACTIVE:
        return emit(record)
    if not record.get("worker_pid"):
        raise Fail("the run hasn't started yet; try again in a few seconds", 1, "error")
    try:
        os.kill(record["worker_pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 40  # stop() gives Claude up to 25s to wind down
    while time.monotonic() < deadline:
        record = refresh(record)
        if record["status"] not in ACTIVE:
            break
        time.sleep(1)
    return emit(record)


def build_parser():
    parser = argparse.ArgumentParser(prog="relay.py", description="Run bounded tasks in a local Claude Code session for Codex.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="check the claude CLI, its version and sign-in without a model call")

    run = sub.add_parser("run", help="start a review or implement run")
    run.add_argument("--mode", choices=("review", "implement"))
    run.add_argument("--brief", required=True, help="path to the brief, or - to read it from stdin")
    run.add_argument("--cwd", help="folder to work in (default: the current folder)")
    run.add_argument("--worktree", metavar="NAME", help="implement: create a git worktree on branch crew/NAME")
    run.add_argument("--base", metavar="REF", help="implement: commit the worktree starts from (default: HEAD)")
    run.add_argument("--model", help="model alias or full name, e.g. sonnet, opus, haiku, fable")
    run.add_argument("--effort", choices=EFFORTS)
    run.add_argument("--allow", action="append", default=[], metavar="RULE",
                     help="extra permission rule, e.g. 'Bash(npm test *)' (repeatable)")
    run.add_argument("--max-turns", type=int, default=50)
    run.add_argument("--max-budget-usd", type=float)
    run.add_argument("--timeout", type=int, default=1800, help="seconds before the relay stops Claude (default: 1800)")
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
    return parser


COMMANDS = {
    "preflight": cmd_preflight,
    "run": cmd_run,
    "status": cmd_status,
    "list": cmd_list,
    "verify": cmd_verify,
    "cancel": cmd_cancel,
    "_worker": cmd_worker,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except Fail as exc:
        return emit({"status": exc.status, "error": str(exc)}, exc.code)


if __name__ == "__main__":
    sys.exit(main())
