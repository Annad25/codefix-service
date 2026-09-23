"""Async subprocess execution with hard timeouts.

Everything that runs a program goes through run_process, so a timeout always
kills the whole process tree (a test runner that forks children must not
survive its budget) and never blocks the event loop.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence


@dataclass(frozen=True)
class ProcResult:
    returncode: int | None      # None when killed on timeout
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0

    @property
    def output(self) -> str:
        """stdout then stderr, as a human would read a terminal."""
        if self.stdout and self.stderr:
            return f"{self.stdout.rstrip()}\n{self.stderr.rstrip()}"
        return (self.stdout or self.stderr).rstrip()


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def run_process(argv: Sequence[str], *, cwd: Path | None = None,
                      env: Mapping[str, str] | None = None, timeout: float,
                      stdin: bytes | None = None, merge_stderr: bool = False,
                      on_timeout: Callable[[], Awaitable[None]] | None = None) -> ProcResult:
    """Run argv, capture output as UTF-8 text, kill the tree if it exceeds timeout.

    on_timeout runs after the local kill; the Docker backend uses it to kill the
    container, because killing the docker CLI alone leaves the container running.
    merge_stderr sends stderr into stdout so output keeps its real interleaving
    (what a model reading a test log needs).
    """
    started = time.monotonic()
    kwargs = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True     # own process group, so killpg reaches children
    else:
        # Taskkill /T only sees descendants reliably when the shell starts in
        # its own Windows process group.  This matters for test runners that
        # background helpers before the parent times out.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd) if cwd else None, env=dict(env) if env is not None else None,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE, **kwargs)
    # communicate() is never cancelled mid-read: after a kill the pipes reach EOF
    # and it finishes normally, so transports are closed cleanly.
    comm = asyncio.ensure_future(proc.communicate(stdin))
    timed_out = False
    try:
        done, _ = await asyncio.wait({comm}, timeout=max(0.01, timeout))
        if not done:
            timed_out = True
            _kill_tree(proc)
            if on_timeout:
                await on_timeout()
            done, _ = await asyncio.wait({comm}, timeout=10)
        if done:
            out, err = comm.result()
        else:                       # something still holds the pipes open; give up on output
            comm.cancel()
            out, err = b"", b""
    except asyncio.CancelledError:
        _kill_tree(proc)
        if on_timeout:
            await asyncio.shield(on_timeout())
        comm.cancel()
        raise
    return ProcResult(
        returncode=None if timed_out else proc.returncode,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=(err or b"").decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_s=round(time.monotonic() - started, 3),
    )


# Git is always invoked with these so behaviour does not depend on the host's
# global config (autocrlf on Windows, filemode, quoted paths, ownership checks).
GIT_CFG = ("-c", "core.autocrlf=false", "-c", "core.filemode=false", "-c", "core.quotepath=false",
           "-c", "core.safecrlf=false", "-c", "safe.directory=*", "-c", "color.ui=false")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "codefix", "GIT_AUTHOR_EMAIL": "codefix@localhost",
    "GIT_COMMITTER_NAME": "codefix", "GIT_COMMITTER_EMAIL": "codefix@localhost",
    "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
}


async def git(args: Sequence[str], cwd: Path, *, timeout: float = 60.0) -> ProcResult:
    env = {**os.environ, **GIT_ENV}
    return await run_process(["git", *GIT_CFG, *args], cwd=cwd, env=env, timeout=timeout)
