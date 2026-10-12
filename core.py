"""Core logic of the sandbox runner.

Kept free of web-framework imports so it can be unit-tested on its own.
app.py exposes these functions as HTTP endpoints for n8n.
"""
import os
import re
import shutil
import signal
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

WORK_ROOT = Path(os.getenv("WORK_ROOT", "/data/workspaces")).resolve()
ARCHIVE_ROOT = Path(os.getenv("ARCHIVE_ROOT", "/data/archives")).resolve()
TEST_TIMEOUT = int(os.getenv("TEST_TIMEOUT_SEC", "100"))     # total budget for all test commands
CLONE_TIMEOUT = int(os.getenv("CLONE_TIMEOUT_SEC", "90"))
MAX_OUTPUT_CHARS = int(os.getenv("MAX_OUTPUT_CHARS", "8000"))
MAX_FILES_PER_CHANGE = 200
MAX_FILE_BYTES = 1_000_000

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", "dist", "build"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")


class RunnerError(Exception):
    """Infrastructure-level problem (maps to an HTTP error status)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- helpers

def _env():
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("SANDBOX_HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "CI": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _clip(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return text[:half] + "\n...[output truncated]...\n" + text[-half:]


def run_cmd(args, cwd, timeout):
    """Run a command in its own process group; kill the whole group on timeout.
    Returns (exit_code, stdout, stderr, timed_out)."""
    proc = subprocess.Popen(
        args, cwd=str(cwd), env=_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=max(1, timeout))
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
        return 124, out or "", (err or "") + f"\n[killed: timeout after {timeout}s]", True


def workspace_path(workspace_id: str) -> Path:
    if not ID_RE.match(workspace_id or ""):
        raise RunnerError(400, "invalid workspace_id")
    ws = WORK_ROOT / workspace_id
    if not ws.is_dir():
        raise RunnerError(404, f"workspace not found: {workspace_id}")
    return ws


def safe_join(root: Path, rel: str) -> Path:
    """Resolve rel inside root or raise ValueError (blocks absolute paths, .., symlink escapes, .git)."""
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("empty path")
    if rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        raise ValueError(f"absolute path not allowed: {rel}")
    parts = Path(rel).parts
    if ".git" in parts:
        raise ValueError(f"path inside .git not allowed: {rel}")
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes workspace: {rel}")
    return target


def list_tree(ws: Path, cap: int = 5000):
    out = []
    for dirpath, dirnames, filenames in os.walk(ws):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, name), ws).replace(os.sep, "/"))
            if len(out) >= cap:
                return sorted(out)
    return sorted(out)


# ---------------------------------------------------------------- endpoints

def prepare(run_id, mode="existing_repo", repo_url="", branch="main"):
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    safe_run = re.sub(r"[^A-Za-z0-9_-]", "", str(run_id))[:40] or "run"
    workspace_id = f"ws-{safe_run}-{uuid.uuid4().hex[:6]}"
    ws = WORK_ROOT / workspace_id

    if mode == "new_project":
        ws.mkdir(parents=True)
        code, _, err, _ = run_cmd(["git", "init", "-q"], ws, 30)
        if code != 0:
            shutil.rmtree(ws, ignore_errors=True)
            raise RunnerError(500, f"git init failed: {err}")
    else:
        if not re.match(r"^https?://", repo_url or ""):
            raise RunnerError(400, "repo_url must start with http:// or https://")
        if not BRANCH_RE.match(branch or ""):
            raise RunnerError(400, "invalid branch name")
        code, _, err, timed_out = run_cmd(
            ["git", "clone", "--depth", "1", "--branch", branch, "--", repo_url, str(ws)],
            WORK_ROOT, CLONE_TIMEOUT)
        if code != 0:
            shutil.rmtree(ws, ignore_errors=True)
            raise RunnerError(502, f"git clone failed: {_clip(err)}")

    run_cmd(["git", "checkout", "-q", "-b", f"agent/{workspace_id}"], ws, 30)
    return {"workspace_id": workspace_id, "mode": mode, "file_tree": list_tree(ws)}


def read_files(workspace_id, paths, max_bytes_per_file=20000):
    ws = workspace_path(workspace_id)
    files, truncated, missing = {}, [], []
    for rel in paths or []:
        try:
            p = safe_join(ws, rel)
        except ValueError:
            missing.append(rel)
            continue
        if not p.is_file():
            missing.append(rel)
            continue
        with open(p, "rb") as fh:
            data = fh.read(max_bytes_per_file + 1)
        if b"\0" in data[:1024]:
            missing.append(rel)          # binary file: not useful as text context
            continue
        if len(data) > max_bytes_per_file:
            data = data[:max_bytes_per_file]
            truncated.append(rel)
        files[rel] = data.decode("utf-8", errors="replace")
    return {"workspace_id": workspace_id, "files": files, "truncated": truncated, "missing": missing}


def _fail(failure_type, message, exit_code=2):
    return {"passed": False, "exit_code": exit_code, "failure_type": failure_type,
            "stdout": "", "stderr": message, "files_written": [], "commands_run": []}


def apply_and_test(workspace_id, changes, test_commands, attempt=0):
    """Write the agent's files, run the test commands. A failing test is NOT an
    error: it returns passed=false with HTTP 200 so the agent can repair it."""
    ws = workspace_path(workspace_id)

    if not changes:
        return _fail("invalid_changes", "No file changes were provided.")
    if len(changes) > MAX_FILES_PER_CHANGE:
        return _fail("invalid_changes", f"Too many files in one change (max {MAX_FILES_PER_CHANGE}).")
    if not test_commands:
        return _fail("invalid_changes", "No test commands were provided; tests must be run.")

    written = []
    for ch in changes:
        try:
            target = safe_join(ws, ch.get("path"))
        except ValueError as exc:
            return _fail("invalid_changes", str(exc))
        content = ch.get("content", "")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return _fail("invalid_changes", f"File too large: {ch.get('path')}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(ch["path"])

    deadline = time.monotonic() + TEST_TIMEOUT
    out_parts, err_parts, ran = [], [], []
    exit_code, timed_out = 0, False
    for cmd in test_commands:
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            exit_code, timed_out = 124, True
            err_parts.append("[total test time budget exhausted before running: %s]" % cmd)
            break
        code, out, err, to = run_cmd(["bash", "-c", cmd], ws, int(remaining))
        ran.append(cmd)
        out_parts.append(f"$ {cmd}\n{out}")
        if err:
            err_parts.append(f"$ {cmd}\n{err}")
        if code != 0:
            exit_code, timed_out = code, to
            break

    if exit_code == 0:
        failure_type = None
    elif timed_out:
        failure_type = "timeout"
    elif exit_code == 127:
        failure_type = "command_not_found"
    else:
        failure_type = "test_failure"

    return {
        "passed": exit_code == 0,
        "exit_code": exit_code,
        "failure_type": failure_type,
        "stdout": _clip("\n".join(out_parts)),
        "stderr": _clip("\n".join(err_parts)),
        "files_written": written,
        "commands_run": ran,
        "attempt": attempt,
    }


def finalize(workspace_id, approved=False, commit_message="AI coding agent changes"):
    if not approved:
        raise RunnerError(400, "finalize requires approved=true")
    ws = workspace_path(workspace_id)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)

    run_cmd(["git", "add", "-A"], ws, 30)
    code, status, _, _ = run_cmd(["git", "status", "--porcelain"], ws, 30)
    commit_sha, diff_stat = None, ""
    if status.strip():
        msg = (commit_message or "AI coding agent changes")[:200]
        code, _, err, _ = run_cmd(
            ["git", "-c", "user.name=coding-agent", "-c", "user.email=agent@localhost",
             "commit", "-q", "-m", msg], ws, 60)
        if code != 0:
            raise RunnerError(500, f"git commit failed: {_clip(err)}")
    code, sha, _, _ = run_cmd(["git", "rev-parse", "HEAD"], ws, 30)
    if code == 0:
        commit_sha = sha.strip()
        _, diff_stat, _, _ = run_cmd(["git", "show", "--stat", "--oneline", "--no-color", "HEAD"], ws, 30)

    archive = ARCHIVE_ROOT / f"{workspace_id}.tar.gz"

    def _filter(info):
        parts = Path(info.name).parts
        return None if any(p in SKIP_DIRS for p in parts) else info

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(ws, arcname=workspace_id, filter=_filter)

    _, branch, _, _ = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], ws, 30)
    return {"workspace_id": workspace_id, "archive_path": str(archive), "commit": commit_sha,
            "branch": branch.strip(), "diff_stat": _clip(diff_stat), "committed": bool(status.strip())}


def cleanup(workspace_id):
    ws = workspace_path(workspace_id)
    shutil.rmtree(ws, ignore_errors=True)
    return {"workspace_id": workspace_id, "deleted": True}
