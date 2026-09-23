"""Deadline arithmetic, and the Docker sandbox's flags against the grader's own argv."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from app.deadline import Deadline
from app.sandbox import DockerSandbox, write_overlay

from conftest import run, task_dir


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_deadline_math():
    clock = FakeClock()
    d = Deadline.after(100, clock=clock)
    assert d.remaining() == 100 and d.timeout(30) == 30
    clock.now += 95
    assert d.timeout(30) == 5 and not d.expired
    assert d.shortened(10).expired
    clock.now += 10
    assert d.remaining() == 0 and d.expired


def test_request_deadline_keeps_a_transfer_margin():
    clock = FakeClock()
    assert Deadline.for_request(180, clock=clock).remaining() == pytest.approx(167.4)   # 7% margin
    assert Deadline.for_request(60, clock=clock).remaining() == 50                      # 10s floor


def test_docker_argv_contains_every_grader_flag_in_order(grade, tmp_path, monkeypatch):
    """Capture the exact argv grade.py would pass to docker and compare with ours."""
    captured = {}

    def fake_run(argv, *a, **kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(grade.subprocess, "run", fake_run)
    repo = shutil.copytree(task_dir("01-pricing-tax") / "repo", tmp_path / "repo")
    grade.run_acceptance(task_dir("01-pricing-tax"), repo, False, "acceptance:latest")
    theirs = captured["argv"]
    ours = DockerSandbox("acceptance:latest").argv(repo, "CMD", "codefix-x")

    # Every option of theirs (flag + value) appears in ours with the same value.
    boolean_flags = {"--rm", "--read-only"}
    i = 2                                           # skip "docker", "run"
    while theirs[i] != "acceptance:latest":
        flag = theirs[i]
        if flag in boolean_flags:
            assert flag in ours
            i += 1
            continue
        value = theirs[i + 1]
        pairs = list(zip(ours, ours[1:]))
        if flag == "-v":
            assert ("-v", f"{repo}:/work:rw") in pairs
        else:
            assert (flag, value) in pairs, (flag, value)
        i += 2
    # Same image and entrypoint shape: image, bash, -c, command.
    assert ours[-4:] == ["acceptance:latest", "bash", "-c", "CMD"]
    assert ours.index("--network") < ours.index("acceptance:latest")


def test_overlay_cannot_escape(tmp_path):
    for bad in ("../x", "/abs", ".git/hooks/pre-commit", "a/../../x"):
        with pytest.raises(ValueError):
            write_overlay(tmp_path, {bad: "x"})
    write_overlay(tmp_path, {"tests/zz_derived_test.py": "ok"})
    assert (tmp_path / "tests/zz_derived_test.py").read_text() == "ok"


@pytest.mark.skipif(os.environ.get("CODEFIX_DOCKER_TESTS") != "1", reason="set CODEFIX_DOCKER_TESTS=1")
def test_docker_sandbox_probe(tmp_path):
    """Run inside the real image: no network, uid 65534, read-only root, writable /work and /tmp."""
    probe = ("set -e; test \"$(id -u)\" = 65534; ! touch /probe 2>/dev/null; touch /tmp/ok; "
             "touch /work/ok; python3 -c \"import socket; s=socket.socket(); s.settimeout(3); "
             "exec('try:\\n s.connect((\\'1.1.1.1\\', 80))\\n raise SystemExit(9)\\nexcept OSError:\\n pass')\"; "
             "python3 --version; node --version; go version; rustc --version; javac -version; gcc --version | head -1")
    (tmp_path / "work").mkdir()
    os.chmod(tmp_path / "work", 0o777)
    r = run(DockerSandbox("acceptance:latest").run(tmp_path / "work", probe, timeout=120))
    assert r.ok, r.output
