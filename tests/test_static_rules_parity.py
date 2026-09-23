"""Our static_check must agree with the grader's on every input."""
from __future__ import annotations

import random

from app.gate.static_rules import static_check

from conftest import FIX_01

HANDMADE = [
    b"",
    FIX_01.encode(),
    FIX_01.replace("\n", "\r\n").encode(),
    b"\xff\xfe not utf-8",
    b"--- a/x\n+++ b/x\n",
    b"diff --git a/x b/x\nGIT binary patch\nliteral 3\n",
    b"diff --git a//etc/passwd b//etc/passwd\n",
    b"diff --git a/../x b/../x\n",
    b"diff --git a/src/../../x b/src/../../x\n",
    b"diff --git a/.git/config b/.git/config\n",
    b"diff --git a/acceptance/t.py b/acceptance/t.py\n",
    b"diff --git a/acceptance_tests/t.py b/acceptance_tests/t.py\n",
    b"diff --git a/acceptance.py b/acceptance.py\n",
    b"diff --git a/link b/link\nnew file mode 120000\n",
    b"diff --git a/my file.py b/my file.py\n",
    b"diff --git a/x b/x\n" + b"+" * 200_001,
    b"diff --git a/x b/x\n" + b"+" * (200_000 - 19),
]


def test_handmade_cases_agree(grade):
    for case in HANDMADE:
        assert static_check(case) == grade.static_check(case), case[:80]


def test_fuzzed_mutations_agree(grade):
    rng = random.Random(1234)
    tokens = [b"diff --git ", b"a/", b"b/", b"../", b"/", b".git/", b"acceptance/", b"\r\n", b"\n",
              b"GIT binary patch", b"new file mode 120000", b" ", b"x.py", b"\xff", b"@@ -1 +1 @@\n"]
    base = FIX_01.encode()
    for _ in range(3000):
        data = bytearray(base)
        for _ in range(rng.randint(1, 4)):
            pos = rng.randint(0, len(data))
            if rng.random() < 0.7:
                data[pos:pos] = rng.choice(tokens)
            else:
                del data[pos:pos + rng.randint(1, 20)]
        case = bytes(data)
        assert static_check(case) == grade.static_check(case), case[:120]
