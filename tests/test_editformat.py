"""SEARCH/REPLACE parsing and application through the jailed tools."""
from __future__ import annotations

import pytest

from app.editformat import EditFormatError, apply_edit_blocks, parse_edit_blocks

from conftest import run

ANSWER = """Requirements:
1. total adds tax

src/pricing/cart.py
<<<<<<< SEARCH
    return round(subtotal(items) * tax_rate, 2)
=======
    return round(subtotal(items) * (1 + tax_rate), 2)
>>>>>>> REPLACE

```python
`src/pricing/new_module.py`
<<<<<<< SEARCH
=======
VALUE = 1
>>>>>>> REPLACE
```
"""


def test_parse_multiple_blocks_with_fences_and_backticks():
    blocks = parse_edit_blocks(ANSWER)
    assert [b.path for b in blocks] == ["src/pricing/cart.py", "src/pricing/new_module.py"]
    assert blocks[0].search == "    return round(subtotal(items) * tax_rate, 2)"
    assert blocks[1].search == "" and blocks[1].replace == "VALUE = 1"


def test_parse_errors():
    assert parse_edit_blocks("no blocks here") == []
    with pytest.raises(EditFormatError, match="divider"):
        parse_edit_blocks("a.py\n<<<<<<< SEARCH\nx\n>>>>>>> REPLACE\n")
    with pytest.raises(EditFormatError, match="REPLACE"):
        parse_edit_blocks("a.py\n<<<<<<< SEARCH\nx\n=======\ny\n")
    with pytest.raises(EditFormatError, match="no file path"):
        parse_edit_blocks("<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n")


def test_apply_blocks_through_the_jail(make_ctx):
    ctx = make_ctx()
    outcomes = run(apply_edit_blocks(ctx, parse_edit_blocks(ANSWER)))
    assert [o.ok for o in outcomes] == [True, True]
    assert b"(1 + tax_rate)" in (ctx.root / "src/pricing/cart.py").read_bytes()
    assert (ctx.root / "src/pricing/new_module.py").read_bytes() == b"VALUE = 1\n"


def test_failed_block_does_not_stop_the_others_and_tests_stay_read_only(make_ctx):
    ctx = make_ctx()
    text = ("tests/test_cart.py\n<<<<<<< SEARCH\n12.0\n=======\n0.0\n>>>>>>> REPLACE\n"
            "src/pricing/cart.py\n<<<<<<< SEARCH\nnot in file\n=======\nx\n>>>>>>> REPLACE\n"
            "src/pricing/cart.py\n<<<<<<< SEARCH\n* tax_rate, 2)\n=======\n* (1 + tax_rate), 2)\n>>>>>>> REPLACE\n")
    outcomes = run(apply_edit_blocks(ctx, parse_edit_blocks(text)))
    assert [o.ok for o in outcomes] == [False, False, True]
    assert "read-only" in outcomes[0].message and "not found" in outcomes[1].message


def test_trailing_whitespace_in_search_is_tolerated(make_ctx):
    ctx = make_ctx()
    text = "src/pricing/cart.py\n<<<<<<< SEARCH\ndef total(items, tax_rate):   \n=======\ndef total(items, tax_rate):\n>>>>>>> REPLACE\n"
    outcomes = run(apply_edit_blocks(ctx, parse_edit_blocks(text)))
    # SEARCH differs only by trailing spaces; replacement equals the original, so it is a no-op edit.
    assert not outcomes[0].ok and "identical" in outcomes[0].message
