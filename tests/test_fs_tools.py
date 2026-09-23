"""File tools: normal use, error paths, and the guarantees the agent relies on."""
from __future__ import annotations

from conftest import run

CART = "src/pricing/cart.py"


def call(registry, ctx, name, **args):
    return run(registry.dispatch(ctx, name, args))


def test_list_dir(registry, make_ctx):
    ctx = make_ctx()
    r = call(registry, ctx, "list_dir", path=".")
    assert not r.is_error
    assert "src/" in r.output and "tests/" in r.output and ".git" not in r.output
    assert call(registry, ctx, "list_dir", path="nope").is_error
    assert call(registry, ctx, "list_dir", path=CART).is_error


def test_read_file_windows_and_line_numbers(registry, make_ctx, settings):
    ctx = make_ctx()
    r = call(registry, ctx, "read_file", path=CART, start_line=None, end_line=None)
    assert r.output.startswith(f"{CART} (lines 1-16 of 16)")
    assert "16|     return round(subtotal(items) * tax_rate, 2)" in r.output
    r = call(registry, ctx, "read_file", path=CART, start_line=15, end_line=99)
    assert r.output.startswith(f"{CART} (lines 15-16 of 16)")
    for bad in ({"start_line": 0, "end_line": None}, {"start_line": 50, "end_line": None},
                {"start_line": 5, "end_line": 2}):
        assert call(registry, ctx, "read_file", path=CART, **bad).is_error


def test_read_file_caps_window_and_hints_next_page(registry, make_ctx):
    ctx = make_ctx()
    (ctx.root / "big.py").write_bytes(("x = 1\n" * 600).encode())
    r = call(registry, ctx, "read_file", path="big.py", start_line=None, end_line=None)
    assert "(lines 1-250 of 600)" in r.output and "start_line=251" in r.output


def test_read_file_refuses_binary_and_outside(registry, make_ctx):
    ctx = make_ctx()
    (ctx.root / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert "binary" in call(registry, ctx, "read_file", path="blob.bin", start_line=None, end_line=None).output
    r = call(registry, ctx, "read_file", path="../pristine/repo/README.md", start_line=None, end_line=None)
    assert r.is_error and "leaves the repository" in r.output


def test_search(registry, make_ctx):
    ctx = make_ctx()
    r = call(registry, ctx, "search", pattern=r"def total", path=None)
    assert r.output == f"{CART}:15: def total(items, tax_rate):"
    assert "no matches" in call(registry, ctx, "search", pattern="zzz_nothing", path="src").output
    assert "invalid regular expression" in call(registry, ctx, "search", pattern="(", path=None).output


def test_str_replace_exactly_once(registry, make_ctx):
    ctx = make_ctx()
    r = call(registry, ctx, "str_replace", path=CART,
             old_str="* tax_rate, 2)", new_str="* (1 + tax_rate), 2)")
    assert not r.is_error and "(1 + tax_rate)" in r.output
    assert ctx.state.edited_files == [CART]
    assert b"\r\n" not in (ctx.root / CART).read_bytes()          # line endings preserved


def test_str_replace_errors_leave_file_untouched(registry, make_ctx):
    ctx = make_ctx()
    before = (ctx.root / CART).read_bytes()
    assert "not found" in call(registry, ctx, "str_replace", path=CART, old_str="nope", new_str="x").output
    assert "matches 2 places" in call(registry, ctx, "str_replace", path=CART, old_str="def ", new_str="def  ").output
    assert "identical" in call(registry, ctx, "str_replace", path=CART, old_str="def", new_str="def").output
    assert "empty" in call(registry, ctx, "str_replace", path=CART, old_str="", new_str="x").output
    r = call(registry, ctx, "str_replace", path=CART, old_str="def total(items, tax_rate):",
             new_str="def total(items, tax_rate)")
    assert r.is_error and "syntax error" in r.output
    assert (ctx.root / CART).read_bytes() == before
    assert ctx.state.edited_files == []


def test_existing_tests_are_read_only(registry, make_ctx):
    ctx = make_ctx()
    r = call(registry, ctx, "str_replace", path="tests/test_cart.py", old_str="12.0", new_str="0.0")
    assert r.is_error and "read-only" in r.output
    r = call(registry, ctx, "write_file", path="tests/test_cart.py", content="")
    assert r.is_error and "read-only" in r.output


def test_str_replace_keeps_crlf_files_crlf(registry, make_ctx):
    ctx = make_ctx()
    (ctx.root / "win.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    r = call(registry, ctx, "str_replace", path="win.txt", old_str="one\ntwo", new_str="uno\ndos")
    assert not r.is_error
    assert (ctx.root / "win.txt").read_bytes() == b"uno\r\ndos\r\nthree\r\n"


def test_write_file(registry, make_ctx):
    ctx = make_ctx()
    r = call(registry, ctx, "write_file", path="src/pricing/extra.py", content="VALUE = 1\n")
    assert r.output == "Created src/pricing/extra.py (1 lines)."
    assert (ctx.root / "src/pricing/extra.py").read_bytes() == b"VALUE = 1\n"
    assert "syntax error" in call(registry, ctx, "write_file", path="bad.py", content="def (\n").output
    assert not (ctx.root / "bad.py").exists()
    assert call(registry, ctx, "write_file", path="acceptance_tests/x.py", content="").is_error
    assert call(registry, ctx, "write_file", path="../escape.py", content="").is_error
