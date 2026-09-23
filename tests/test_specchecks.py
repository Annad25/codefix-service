"""Derived-check answers: the format variants weaker models produce, and the retry on bad format."""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.deadline import Deadline
from app.llm import LLMClient
from app.profiler import profile_repo
from app.sandbox import make_sandbox
from app.specchecks import SpecFormatError, generate_spec_checks, parse_spec_answer
from app.workspace import Workspace

from conftest import run, sse_response

CODE = "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"
CMD = "PYTHONPATH=src python3 -m unittest tests.zz_derived_test -v"


@pytest.mark.parametrize("answer", [
    f"FILE: tests/zz_derived_test.py\n```python\n{CODE}```\nCOMMAND: {CMD}\n",
    f"**FILE:** `tests/zz_derived_test.py`\n\n```python\n{CODE}```\n\n**COMMAND:** `{CMD}`\n",
    f"### File: tests/zz_derived_test.py\n```\n{CODE}```\nCommand:\n```bash\n{CMD}\n```\n",
    f"### tests/zz_derived_test.py\n```python\n{CODE}```\nRun with: `{CMD}`\n",
    f"Here are the tests.\n```python\n# tests/zz_derived_test.py\n{CODE}```\nCOMMAND: $ {CMD}\n",
])
def test_format_variants_parse(answer):
    checks = parse_spec_answer(answer)
    assert list(checks.files) == ["tests/zz_derived_test.py"] and checks.command == CMD
    assert "class T" in checks.files["tests/zz_derived_test.py"]


@pytest.mark.parametrize("answer, message", [
    ("no code at all", "no fenced code block"),
    (f"```python\n{CODE}```\nCOMMAND: {CMD}", "no FILE"),
    (f"FILE: tests/zz_derived_test.py\n```python\n{CODE}```\n", "no COMMAND"),
    (f"FILE: tests/test_cart.py\n```python\n{CODE}```\nCOMMAND: {CMD}", "not allowed"),
    (f"FILE: ../zz_derived.py\n```python\n{CODE}```\nCOMMAND: {CMD}", "not allowed"),
    (f"FILE: tests/zz_derived_test.py\n```python\n{CODE}```\nCOMMAND: true", "no-op"),
])
def test_bad_answers_are_rejected(answer, message):
    with pytest.raises(SpecFormatError, match=message):
        parse_spec_answer(answer)


def test_malformed_answer_gets_one_retry_with_the_problem_stated(task_archive, tmp_path):
    answers = ["Sure! Tests:\n```python\nprint('no path')\n```",
               "FILE: tests/zz_derived_test.py\n```python\n"
               "import unittest\nfrom pricing.cart import total\n\n"
               "class D(unittest.TestCase):\n    def test_empty(self):\n        self.assertEqual(total([], 0.2), 0.0)\n"
               "```\nCOMMAND: PYTHONPATH=src python3 -m unittest tests.zz_derived_test -v\n"]
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return sse_response({"id": "g", "object": "chat.completion", "created": 1, "model": "m",
                             "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": answers[len(requests) - 1]}}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0}})
    settings = Settings(llm_api_key="sk-or-test", sandbox_backend="local")
    llm = LLMClient(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ws = Workspace.create(task_archive("01-pricing-tax"), tmp_path / "w", "spec")
    try:
        result = run(generate_spec_checks(
            llm, model="m", fallback_models=[], task="t", profile=profile_repo(ws.pristine),
            deadline=Deadline.after(120), ledger=llm.new_request_ledger(), sandbox=make_sandbox(settings),
            scratch_parent=ws.scratch_dir(), pristine=ws.pristine))
    finally:
        ws.cleanup()
    assert result.checks is not None and len(requests) == 2 and len(result.answers) == 2
    retry_prompt = requests[1]["messages"][-1]["content"]
    assert "could not be used" in retry_prompt and "FILE:" in retry_prompt
    assert result.note.startswith("derived checks ready")
