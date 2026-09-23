"""The full pipeline against a scripted fake OpenRouter: no network, no spend.

Covers first-try success, repair after a failing test, unparseable answers,
derived-check failures, spend caps, bad archives and the watchdog. Every
delivered diff is also checked with the grader's own static_check.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app.config import Settings
from app.llm import LLMClient
from app.prompts import SPEC_SYSTEM
from app.sandbox import make_sandbox
from app.solver import Solver, SolveRequest

from conftest import run, sse_response

FIX_RIGHT = """1. total = subtotal + tax, rounded to 2 decimals

src/pricing/cart.py
<<<<<<< SEARCH
    return round(subtotal(items) * tax_rate, 2)
=======
    return round(subtotal(items) * (1 + tax_rate), 2)
>>>>>>> REPLACE
"""
FIX_WRONG = FIX_RIGHT.replace("(1 + tax_rate)", "(2 + tax_rate)")
REPAIR_RIGHT = """src/pricing/cart.py
<<<<<<< SEARCH
    return round(subtotal(items) * (2 + tax_rate), 2)
=======
    return round(subtotal(items) * (1 + tax_rate), 2)
>>>>>>> REPLACE
"""

SPEC_TEMPLATE = """FILE: tests/zz_derived_test.py
```python
import unittest
from pricing.cart import Item, total


class Derived(unittest.TestCase):
    def test_empty_cart(self):
        self.assertEqual(total([], 0.2), {empty})

    def test_zero_rate(self):
        self.assertEqual(total([Item("a", 3.0, 2)], 0), 6.0)

    def test_tax(self):
        self.assertEqual(total([Item("a", 10.0)], 0.2), 12.0)
```
COMMAND: PYTHONPATH=src python3 -m unittest tests.zz_derived_test -v
"""
SPEC_RIGHT = SPEC_TEMPLATE.format(empty="0.0")
SPEC_WRONG = SPEC_TEMPLATE.format(empty="1.0")        # contradicts the task: empty cart must be 0.0


class FakeOpenRouter:
    """Answers spec prompts with `spec` and fix prompts from the `fixes` queue."""

    def __init__(self, fixes, spec=SPEC_RIGHT, cost=0.001, delay=0.0, finish_reasons=None):
        self.finish_reasons = list(finish_reasons or [])
        self.fixes = list(fixes)
        self.spec = spec
        self.cost = cost
        self.delay = delay
        self.fix_requests: list[dict] = []
        self.spec_requests: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if self.delay:
            await asyncio.sleep(self.delay)
        finish = "stop"
        if body["messages"][0]["content"] == SPEC_SYSTEM:
            self.spec_requests.append(body)
            content = self.spec
        else:
            self.fix_requests.append(body)
            content = self.fixes.pop(0) if self.fixes else "I have nothing more to add."
            if self.finish_reasons:
                finish = self.finish_reasons.pop(0)
        return sse_response({
            "id": "gen", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200, "cost": self.cost},
        })


def make_solver(tmp_path, fake, **overrides):
    settings = Settings(llm_api_key="sk-or-test", sandbox_backend="local", work_root=tmp_path / "work",
                        trace_dir=tmp_path / "runs", model_primary="m/primary", model_secondary="m/secondary",
                        model_spec="m/spec", **overrides)
    llm = LLMClient(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    return Solver(settings, llm, make_sandbox(settings))


def solve(solver, archive, deadline=180, task="Fix total() so it returns subtotal plus tax."):
    return run(solver.solve(SolveRequest(request_id="t-01", archive=archive, task=task, deadline_seconds=deadline)))


def test_first_try_success_is_tier_a_and_well_formed(grade, task_archive, tmp_path):
    fake = FakeOpenRouter([FIX_RIGHT])
    result = solve(make_solver(tmp_path, fake), task_archive("01-pricing-tax"))
    assert result.meta["tier"] == "A" and result.meta["rounds"] == {"c1": 1}
    assert grade.static_check(result.diff.encode()) is None
    assert "(1 + tax_rate)" in result.diff and "zz_derived" not in result.diff    # overlay never delivered
    kinds = [(e["role"], e.get("type"), e.get("name")) for e in result.record]
    assert kinds[:3] == [("assistant", "tool_call", "write_task_checks"), ("assistant", "text", None),
                         ("tool", None, "write_task_checks")]                # the spec model's answer is recorded
    assert ("tool", None, "delivery_gate") in kinds and ("assistant", "text", None) in kinds
    assert result.usage["calls"] == 2 and result.usage["estimated_cost_usd"] == pytest.approx(0.002)
    assert len(fake.fix_requests) == 1                                  # no second candidate after tier A
    assert list((tmp_path / "runs").glob("*.json"))                     # trace written


def test_failing_test_is_fed_back_and_repaired(task_archive, tmp_path):
    fake = FakeOpenRouter([FIX_WRONG, REPAIR_RIGHT])
    result = solve(make_solver(tmp_path, fake), task_archive("01-pricing-tax"))
    assert result.meta["tier"] == "A" and result.meta["rounds"] == {"c1": 2}
    repair_prompt = fake.fix_requests[1]["messages"][-1]["content"]
    assert "did not pass verification" in repair_prompt
    assert "(2 + tax_rate)" in repair_prompt                           # current file content included
    assert "FAILED" in repair_prompt


def test_unparseable_answer_gets_format_feedback(task_archive, tmp_path):
    fake = FakeOpenRouter(["here is my fix: change tax_rate", FIX_RIGHT])
    result = solve(make_solver(tmp_path, fake), task_archive("01-pricing-tax"))
    assert result.meta["tier"] == "A"
    assert "No SEARCH/REPLACE blocks" in fake.fix_requests[1]["messages"][-1]["content"]


def test_wrong_derived_check_gives_tier_b_and_the_model_can_push_back(task_archive, tmp_path):
    """A derived check can itself be wrong; the model may say so instead of bending the code."""
    fake = FakeOpenRouter([FIX_RIGHT, "DERIVED_CHECK_WRONG\nThe task says an empty cart totals 0.0."],
                          spec=SPEC_WRONG)
    result = solve(make_solver(tmp_path, fake, max_candidates=1), task_archive("01-pricing-tax"))
    assert result.meta["tier"] == "B" and "(1 + tax_rate)" in result.diff
    assert any("judged a derived check wrong" in n for n in result.meta["notes"])
    repair_prompt = fake.fix_requests[1]["messages"][-1]["content"]
    assert "def test_empty_cart" in repair_prompt          # the failing test's source is shown, not just its output


def test_spend_cap_stops_further_calls_and_returns_null(task_archive, tmp_path):
    fake = FakeOpenRouter([FIX_WRONG, REPAIR_RIGHT], cost=0.5)
    result = solve(make_solver(tmp_path, fake, max_cost_per_request_usd=0.6), task_archive("01-pricing-tax"))
    assert result.diff is None
    assert len(fake.fix_requests) == 1                                  # repair call refused before sending
    assert any("spend cap" in n for n in result.meta["notes"])


def test_bad_archive_returns_null_quickly(tmp_path):
    fake = FakeOpenRouter([FIX_RIGHT])
    started = time.monotonic()
    result = solve(make_solver(tmp_path, fake), b"not a tarball")
    assert result.diff is None and time.monotonic() - started < 2
    assert "rejected snapshot" in result.record[0]["text"] and fake.fix_requests == []


def test_watchdog_returns_before_the_deadline(task_archive, tmp_path, monkeypatch):
    solver = make_solver(tmp_path, FakeOpenRouter([]))

    async def hang(*args, **kwargs):
        await asyncio.sleep(3600)
    monkeypatch.setattr(solver, "_run_candidate", hang)
    started = time.monotonic()
    result = solve(solver, task_archive("01-pricing-tax"), deadline=13)     # hard deadline = 13 - 10s margin
    elapsed = time.monotonic() - started
    assert result.diff is None and elapsed < 5
    assert any("watchdog" in n for n in result.meta["notes"])


def test_solver_honours_the_client_deadline(task_archive, tmp_path, monkeypatch):
    solver = make_solver(tmp_path, FakeOpenRouter([FIX_RIGHT]))
    seen = {}
    real = solver._solve

    async def spy(req, hard, state):
        seen["remaining"] = hard.remaining()
        return await real(req, hard, state)
    monkeypatch.setattr(solver, "_solve", spy)
    result = solve(solver, task_archive("01-pricing-tax"), deadline=13)
    assert seen["remaining"] < 5 and result.diff is None


def test_budget_floor_extends_a_short_deadline(task_archive, tmp_path, monkeypatch):
    """Evaluation mode: the floor gives slow models more time than deadline_seconds."""
    solver = make_solver(tmp_path, FakeOpenRouter([FIX_RIGHT]), budget_floor_s=600)
    seen = {}
    real = solver._solve

    async def spy(req, hard, state):
        seen["remaining"] = hard.remaining()
        return await real(req, hard, state)
    monkeypatch.setattr(solver, "_solve", spy)
    result = solve(solver, task_archive("01-pricing-tax"), deadline=20)
    assert seen["remaining"] > 500 and result.meta["tier"] == "A"


def test_a_truncated_fix_answer_is_retried_with_a_shorter_format(task_archive, tmp_path):
    """An answer cut off before any edit block must not waste the round."""
    fake = FakeOpenRouter(["1. requirement\n2. requirement\n(cut off mid-sentence", FIX_RIGHT],
                          finish_reasons=["length", "stop"])
    result = solve(make_solver(tmp_path, fake), task_archive("01-pricing-tax"))
    assert result.meta["tier"] == "A"
    assert "cut off at the output limit" in fake.fix_requests[1]["messages"][-1]["content"]


def test_failing_derived_checks_still_reach_the_gate_and_deliver_tier_b(task_archive, tmp_path):
    """Repo tests passing is enough to be delivered when no Tier A exists (regression)."""
    fake = FakeOpenRouter([FIX_RIGHT, "no more changes needed", "no more changes needed"], spec=SPEC_WRONG)
    result = solve(make_solver(tmp_path, fake, max_candidates=1), task_archive("01-pricing-tax"))
    assert result.meta["tier"] == "B" and result.diff and "(1 + tax_rate)" in result.diff
    names = [e.get("name") for e in result.record]
    assert "delivery_gate" in names          # the gate ran despite the derived failure
