"""The path jail: nothing outside the repository, ever."""
from __future__ import annotations

import os

import pytest

from app.tools.jail import JailError, PathJail


@pytest.fixture
def jail(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("")
    (root / ".git").mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    return PathJail(root, frozenset({"tests/test_a.py"}))


@pytest.mark.parametrize("path", [
    "../secret.txt", "src/../../secret.txt", "/etc/passwd", "C:/Windows/win.ini", "C:\\x",
    "\\\\server\\share", "~/.ssh/id_rsa", ".git/config", "src/.git/x", "acceptance/t.py",
    "acceptance_tests/t.py", "a\x00b",
])
def test_escapes_and_restricted_paths_are_refused(jail, path):
    with pytest.raises(JailError):
        jail.resolve(path)


def test_normal_paths_resolve(jail):
    path, norm = jail.resolve("./src//a.py")
    assert norm == "src/a.py" and path.read_text() == "x = 1\n"
    assert jail.resolve(".")[1] == "."
    assert jail.resolve("src\\a.py")[1] == "src/a.py"


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs privileges on Windows")
def test_symlink_components_are_refused(jail, tmp_path):
    os.symlink(tmp_path, jail.root / "src" / "up")
    with pytest.raises(JailError, match="symbolic link"):
        jail.resolve("src/up/secret.txt")


def test_write_rules(jail):
    with pytest.raises(JailError, match="read-only"):
        jail.resolve_for_write("tests/test_a.py")
    with pytest.raises(JailError, match="reserved"):
        jail.resolve_for_write("tests/zz_acceptance.rs")
    with pytest.raises(JailError, match="reserved"):
        jail.resolve_for_write("zz_derived_test.py")
    with pytest.raises(JailError, match="directory"):
        jail.resolve_for_write("src")
    with pytest.raises(JailError, match="root"):
        jail.resolve_for_write(".")
    assert jail.resolve_for_write("tests/test_new.py")[1] == "tests/test_new.py"   # new tests allowed
    assert jail.resolve_for_write("src/a.py")[1] == "src/a.py"
