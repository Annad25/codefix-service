"""Deterministic facts about a repository snapshot, no LLM involved.

The request carries no language or test command, so both are discovered here.
Every task repository ships `run_tests.sh`; manifests are the fallback.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Versions fixed by the acceptance image (acceptance/Dockerfile, Ubuntu 24.04).
TOOLCHAIN_NOTES = {
    "python": "Python 3.12, standard library only, tests use unittest.",
    "javascript": "Node.js 18 (CommonJS, node:test). No npm packages, no network.",
    "typescript": "Node.js 18 with tsc 5.6. No npm packages, no network.",
    "go": "Go 1.22, standard library only.",
    "rust": "Rust 1.75 / cargo --offline, std only. Avoid APIs stabilised after 1.75.",
    "java": "Java 21 (javac/java), standard library only, no build tool.",
    "c": "GCC 13, C11, -Wall -Wextra.",
    "cpp": "GCC 13 (g++).",
    "bash": "GNU bash 5.2 and coreutils.",
}

EXT_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".sh": "bash",
}

SKIP_DIRS = {".git", "node_modules", "target", "build", "out", "__pycache__", ".venv", "dist"}

_TEST_DIR_NAMES = {"test", "tests", "__tests__", "spec", "specs", "testdata"}
_TEST_FILE_PATTERNS = [
    re.compile(p) for p in (
        r"^test_.*\.py$", r"^.*_test\.py$", r"^conftest\.py$",
        r"^.*_test\.go$",
        r"^.*\.(test|spec)\.(js|mjs|cjs|ts)$",
        r"^.*Test\.java$", r"^.*Tests\.java$",
        r"^test_.*\.(c|cc|cpp)$",
        r"^run_tests\.sh$",
    )
]


def is_test_path(rel: str) -> bool:
    """Heuristic: is this repository path part of the test suite?"""
    parts = PurePosixPath(rel).parts
    if any(p in _TEST_DIR_NAMES for p in parts[:-1]):
        return True
    return any(p.match(parts[-1]) for p in _TEST_FILE_PATTERNS)


def iter_files(root: Path):
    """Yield repository-relative POSIX paths of regular files, skipping VCS and build dirs."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            yield full.relative_to(root).as_posix()


def _has_make_test_target(makefile: Path) -> bool:
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"^test\s*:", text, re.MULTILINE) is not None


def _find_java_test_main(test_root: Path) -> str | None:
    """Fully qualified name of the first *Test class under test_root that has a main()."""
    for path in sorted(test_root.rglob("*Test*.java")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "static void main" in text:
            pkg = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.MULTILINE)
            return f"{pkg.group(1)}.{path.stem}" if pkg else path.stem
    return None


def detect_test_command(root: Path) -> tuple[str | None, str]:
    """Return (command, how it was found). Command is None if nothing fits."""
    if (root / "run_tests.sh").is_file():
        return "bash run_tests.sh", "run_tests.sh"
    if (root / "Makefile").is_file() and _has_make_test_target(root / "Makefile"):
        return "make -s test", "Makefile test target"
    if (root / "go.mod").is_file():
        return "go test ./...", "go.mod"
    if (root / "Cargo.toml").is_file():
        return "cargo test --offline", "Cargo.toml"
    if (root / "package.json").is_file():
        if (root / "tsconfig.json").is_file():
            return "tsc -p . && node --test", "package.json + tsconfig.json"
        return "node --test", "package.json"
    if (root / "src" / "test" / "java").is_dir():
        main_class = _find_java_test_main(root / "src" / "test" / "java")
        if main_class:
            return (f"rm -rf out && javac -d out $(find src -name '*.java') && "
                    f"java -cp out {main_class}", "java test class with main()")
        return None, "java sources without a known runner"
    if (root / "tests" / "run.sh").is_file():
        return "bash tests/run.sh", "tests/run.sh"
    if (root / "tests").is_dir() and any(root.glob("tests/test_*.py")):
        pythonpath = "PYTHONPATH=src " if (root / "src").is_dir() else ""
        return f"{pythonpath}python3 -m unittest discover -s tests -t . -v", "python tests/"
    return None, "no test runner found"


def detect_language(root: Path, files: list[str]) -> str:
    if (root / "go.mod").is_file():
        return "go"
    if (root / "Cargo.toml").is_file():
        return "rust"
    if (root / "tsconfig.json").is_file():
        return "typescript"
    if (root / "package.json").is_file():
        return "javascript"
    counts = Counter(EXT_LANGUAGE[Path(f).suffix] for f in files
                     if Path(f).suffix in EXT_LANGUAGE and f != "run_tests.sh")
    return counts.most_common(1)[0][0] if counts else "unknown"


@dataclass
class RepoProfile:
    root: Path
    test_command: str | None
    test_command_source: str
    language: str
    files: list[str]
    test_files: list[str] = field(default_factory=list)

    @property
    def source_files(self) -> list[str]:
        tests = set(self.test_files)
        return [f for f in self.files if f not in tests]

    @property
    def toolchain_note(self) -> str:
        return TOOLCHAIN_NOTES.get(self.language, "See the repository for its toolchain.")

    def repo_map(self, max_entries: int = 200) -> str:
        lines = []
        for rel in self.files[:max_entries]:
            size = (self.root / rel).stat().st_size
            tag = "  [test]" if rel in self.test_files else ""
            lines.append(f"{rel} ({size} B){tag}")
        if len(self.files) > max_entries:
            lines.append(f"... {len(self.files) - max_entries} more files")
        return "\n".join(lines)


def profile_repo(root: Path) -> RepoProfile:
    files = list(iter_files(root))
    command, source = detect_test_command(root)
    return RepoProfile(
        root=root, test_command=command, test_command_source=source,
        language=detect_language(root, files), files=files,
        test_files=[f for f in files if is_test_path(f)],
    )
