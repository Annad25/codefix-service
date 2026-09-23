"""Where repository code runs.

DockerSandbox reproduces the grader's `docker run` (harness/grade.py) flag for
flag, and adds hardening that does not change behaviour for an unprivileged
process (--cap-drop ALL, no-new-privileges). It is the only backend that
satisfies the task's environment-parity requirement.

LocalSandbox runs the command with the host's bash. It is for developing the
service on a machine without Docker and must not be used for real runs.

Callers never run commands in a worktree directly: run_in_copy() gives every
run a disposable copy, so build artifacts never reach the agent's tree or the
diff.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Mapping, Protocol

from .config import Settings, find_bash
from .procs import ProcResult, run_process
from .workspace import copy_tree, rmtree_force

# Mirrors run_acceptance() in harness/grade.py.
GRADE_ENV = {
    "HOME": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
    "GOCACHE": "/tmp/gocache",
    "GOPATH": "/tmp/gopath",
    "GOFLAGS": "-mod=mod",
    "CARGO_HOME": "/tmp/cargo",
    "npm_config_cache": "/tmp/npm",
}


class Sandbox(Protocol):
    name: str

    async def run(self, workdir: Path, command: str, *, timeout: float) -> ProcResult: ...


class DockerSandbox:
    name = "docker"

    def __init__(self, image: str) -> None:
        self.image = image

    def argv(self, workdir: Path, command: str, container_name: str) -> list[str]:
        env_flags: list[str] = []
        for key, value in GRADE_ENV.items():
            env_flags += ["-e", f"{key}={value}"]
        return [
            "docker", "run", "--rm", "--name", container_name, "--label", "codefix=1",
            # exactly the grader's flags
            "--network", "none", "--memory", "1g", "--cpus", "2", "--pids-limit", "512",
            "--read-only", "--tmpfs", "/tmp:exec",
            "-v", f"{workdir}:/work:rw", "-w", "/work",
            *env_flags,
            # hardening that does not change behaviour for uid 65534
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            self.image, "bash", "-c", command,
        ]

    async def run(self, workdir: Path, command: str, *, timeout: float) -> ProcResult:
        container = f"codefix-{uuid.uuid4().hex[:12]}"

        async def kill_container() -> None:
            await run_process(["docker", "kill", container], timeout=15)

        return await run_process(self.argv(workdir, command, container), timeout=timeout,
                                 merge_stderr=True, on_timeout=kill_container)


class LocalSandbox:
    """Development only: runs on the host, with no network or resource isolation."""

    name = "local"

    def __init__(self, bash: str | None = None) -> None:
        self.bash = bash or find_bash()

    async def run(self, workdir: Path, command: str, *, timeout: float) -> ProcResult:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
               "PYTHONIOENCODING": "utf-8"}
        if os.name == "nt":
            # The real Git Bash executable does not add its own usr/bin to
            # PATH (unlike the bin/ launcher).  Preserve the normal Windows
            # PATH while making coreutils such as sleep available.
            env["PATH"] = str(Path(self.bash).parent) + os.pathsep + env.get("PATH", "")
        # Git Bash on Windows commonly exposes the interpreter as `python`
        # only.  This keeps the development-only backend compatible with the
        # Linux-oriented repository test commands; Docker always has python3.
        if os.name == "nt" and shutil.which("python3") is None and shutil.which("python"):
            command = 'python3() { python "$@"; }; export -f python3; ' + command
        return await run_process([self.bash, "-c", command], cwd=workdir, env=env, timeout=timeout,
                                  merge_stderr=True)


class LimitedSandbox:
    """Bulkhead: caps concurrent runs so parallel requests cannot oversubscribe the host."""

    def __init__(self, inner: Sandbox, limit: int) -> None:
        self.inner = inner
        self.name = inner.name
        self._sem = asyncio.Semaphore(max(1, limit))

    async def run(self, workdir: Path, command: str, *, timeout: float) -> ProcResult:
        started = time.monotonic()
        async with self._sem:
            remaining = timeout - (time.monotonic() - started)     # queueing time counts against the budget
            return await self.inner.run(workdir, command, timeout=max(0.01, remaining))


def make_sandbox(settings: Settings) -> Sandbox:
    if settings.sandbox_backend == "docker":
        inner: Sandbox = DockerSandbox(settings.sandbox_image)
    elif settings.sandbox_backend == "local":
        inner = LocalSandbox()
    else:
        raise ValueError(f"unknown sandbox backend: {settings.sandbox_backend}")
    return LimitedSandbox(inner, settings.sandbox_concurrency)


def _make_world_writable(root: Path) -> None:
    """The container runs as uid 65534, which must be able to write build outputs."""
    if os.name == "nt":
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            p = os.path.join(dirpath, name)
            os.chmod(p, os.stat(p).st_mode | stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
        for name in filenames:
            p = os.path.join(dirpath, name)
            mode = os.stat(p).st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP \
                | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH
            os.chmod(p, mode)
    os.chmod(root, os.stat(root).st_mode | stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)


def write_overlay(root: Path, files: Mapping[str, str | bytes]) -> None:
    """Write extra files (e.g. derived checks) into a copy. Paths must stay inside root."""
    base = root.resolve()
    for rel, content in files.items():
        parts = PurePosixPath(rel.replace("\\", "/")).parts
        if not parts or rel.startswith("/") or ".." in parts or parts[0] == ".git":
            raise ValueError(f"overlay path not allowed: {rel!r}")
        target = (base / Path(*parts)).resolve()
        if base not in target.parents:
            raise ValueError(f"overlay path escapes the repository: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


async def run_in_copy(sandbox: Sandbox, src: Path, command: str, *, timeout: float,
                      scratch_parent: Path,
                      overlay: Mapping[str, str | bytes] | None = None) -> ProcResult:
    """Run command against a disposable copy of src (plus optional overlay files)."""
    scratch_parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="run-", dir=scratch_parent))
    try:
        work = copy_tree(src, tmp / "work")
        if overlay:
            write_overlay(work, overlay)
        _make_world_writable(work)
        return await sandbox.run(work, command, timeout=timeout)
    finally:
        rmtree_force(tmp)
