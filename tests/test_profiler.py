"""Test-command and language detection, checked against the public tasks' task.json."""
from __future__ import annotations

import json
import shutil

import pytest

from app.profiler import detect_test_command, is_test_path, profile_repo

from conftest import PUBLIC_TASKS, task_dir


def meta(task: str) -> dict:
    return json.loads((task_dir(task) / "task.json").read_text())


@pytest.mark.parametrize("task", PUBLIC_TASKS)
def test_public_tasks_use_run_tests_and_language_matches(grade, task):
    profile = profile_repo(task_dir(task) / "repo")
    assert profile.test_command == "bash run_tests.sh"
    assert profile.language == meta(task)["language"]
    assert profile.test_files, "every task has tests"
    assert not any(f.startswith("src/") and "test" not in f for f in profile.test_files)


@pytest.mark.parametrize("task", PUBLIC_TASKS)
def test_fallback_without_run_tests_matches_task_json(grade, task, tmp_path):
    repo = shutil.copytree(task_dir(task) / "repo", tmp_path / "repo")
    (repo / "run_tests.sh").unlink()
    command, _ = detect_test_command(repo)
    expected = meta(task)["test_command"]
    if meta(task)["language"] == "javascript":
        assert command == "node --test"          # node discovers tests/*.test.js itself
    else:
        assert command == expected


def test_java_fallback_finds_the_test_main(tmp_path):
    test_dir = tmp_path / "src/test/java/roman"
    test_dir.mkdir(parents=True)
    (test_dir / "RomanNumeralsTest.java").write_text(
        "package roman;\npublic class RomanNumeralsTest { public static void main(String[] a) {} }\n")
    command, _ = detect_test_command(tmp_path)
    assert command == ("rm -rf out && javac -d out $(find src -name '*.java') && "
                       "java -cp out roman.RomanNumeralsTest")


def test_rust_and_nothing(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_test_command(tmp_path)[0] == "cargo test --offline"
    (tmp_path / "Cargo.toml").unlink()
    assert detect_test_command(tmp_path)[0] is None


@pytest.mark.parametrize("path, expected", [
    ("tests/test_cart.py", True), ("ring/ring_test.go", True), ("tests/parseDuration.test.js", True),
    ("src/test/java/roman/Check.java", True), ("tests/helper.sh", True), ("run_tests.sh", True),
    ("src/pricing/cart.py", False), ("ring/ring.go", False), ("src/testing_utils_impl.c", False),
    ("src/main/java/roman/RomanNumerals.java", False),
])
def test_is_test_path(path, expected):
    assert is_test_path(path) is expected
