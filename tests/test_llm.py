"""LLM client against a fake OpenRouter (httpx.MockTransport): no network, no spend."""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import ConfigError, Settings
from app.deadline import Deadline
from app.envfile import load_env_file
from app.llm import BudgetExceeded, CostLedger, LLMClient, LLMUnavailable
from app.tools import default_registry

from conftest import run, sse_chunks, sse_response

KEY = "sk-or-v1-test"


def completion(*, content="ok", tool_calls=None, cost=0.0123, reasoning_details=None, choices=True):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_details:
        message["reasoning_details"] = reasoning_details
    return {
        "id": "gen-1", "object": "chat.completion", "created": 1, "model": "openai/gpt-5.6-terra",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}] if choices else [],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500, "cost": cost,
                  "prompt_tokens_details": {"cached_tokens": 1024},
                  "completion_tokens_details": {"reasoning_tokens": 120}},
    }


class FakeOpenRouter:
    def __init__(self, responses):
        self.responses = list(responses)     # (status, body, headers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body, headers = self.responses.pop(0)
        if status == 200:
            return sse_response(body)
        return httpx.Response(status, json=body, headers=headers or {})

    @property
    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def make_client(responses, **overrides):
    fake = FakeOpenRouter(responses)
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
    settings = Settings(llm_api_key=KEY, **overrides)
    client = LLMClient(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
                       sleep=fake_sleep)
    return client, fake, sleeps


def chat(client, ledger=None, deadline_s=120, **kw):
    ledger = ledger or client.new_request_ledger()
    return run(client.chat(model="openai/gpt-5.6-terra", messages=[{"role": "user", "content": "hi"}],
                           deadline=Deadline.after(deadline_s), ledger=ledger, **kw)), ledger


def test_success_parses_usage_cost_and_sends_safe_routing():
    client, fake, _ = make_client([(200, completion(), None)])
    tools = default_registry().chat_tools()
    reply, ledger = chat(client, tools=tools)
    assert reply.content == "ok"
    u = reply.usage
    assert (u.prompt_tokens, u.completion_tokens, u.cached_tokens, u.reasoning_tokens) == (1200, 300, 1024, 120)
    assert u.cost_usd == pytest.approx(0.0123) and u.cost_known
    assert ledger.spent_usd == pytest.approx(0.0123)
    assert client.global_ledger.spent_usd == pytest.approx(0.0123)

    req, body = fake.requests[0], fake.bodies[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["x-title"] == "codefix-service"
    assert body["provider"] == {"data_collection": "deny", "require_parameters": True, "allow_fallbacks": True}
    assert body["usage"] == {"include": True} and body["reasoning"] == {"effort": "medium"}
    assert body["max_tokens"] == 16_000 and body["tool_choice"] == "auto"
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert [t["function"]["name"] for t in body["tools"]][:2] == ["list_dir", "read_file"]


def test_tool_calls_and_reasoning_details_round_trip():
    tc = [{"id": "call_1", "type": "function",
           "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]
    details = [{"type": "reasoning.encrypted", "data": "opaque"}]
    client, _, _ = make_client([(200, completion(content=None, tool_calls=tc, reasoning_details=details), None)])
    reply, _ = chat(client)
    assert [(t.id, t.name, t.arguments) for t in reply.tool_calls] == [("call_1", "read_file", "{\"path\": \"a.py\"}")]
    assert reply.message["tool_calls"][0]["function"]["name"] == "read_file"
    assert reply.message["reasoning_details"] == details


def test_429_is_retried_honouring_retry_after():
    client, fake, sleeps = make_client([
        (429, {"error": {"message": "slow down"}}, {"retry-after": "2"}),
        (200, completion(), None),
    ])
    reply, _ = chat(client)
    assert reply.attempts == 2 and len(fake.requests) == 2 and sleeps == [2.0]


def test_empty_choices_and_5xx_are_retried_then_give_up():
    client, fake, sleeps = make_client([(200, completion(choices=False), None)] +
                                       [(503, {"error": {"message": "down"}}, None)] * 3)
    with pytest.raises(LLMUnavailable, match="after retries"):
        chat(client)
    assert len(fake.requests) == 4 and len(sleeps) == 3        # max_retries=3


def test_402_stops_immediately_as_budget_exceeded():
    client, fake, sleeps = make_client([(402, {"error": {"message": "Insufficient credits"}}, None)])
    with pytest.raises(BudgetExceeded, match="402"):
        chat(client)
    assert len(fake.requests) == 1 and sleeps == []


def test_client_errors_are_not_retried():
    client, fake, _ = make_client([(400, {"error": {"message": "bad request"}}, None)])
    with pytest.raises(LLMUnavailable, match="400"):
        chat(client)
    assert len(fake.requests) == 1


def test_request_cap_blocks_the_next_call_without_sending_it():
    client, fake, _ = make_client([(200, completion(cost=0.02), None)])
    ledger = CostLedger("request", 0.01)
    chat(client, ledger=ledger)
    with pytest.raises(BudgetExceeded, match="request spend cap"):
        chat(client, ledger=ledger)
    assert len(fake.requests) == 1                             # second call never left the process


def test_process_cap_blocks_across_requests():
    client, fake, _ = make_client([(200, completion(cost=0.03), None)], max_cost_total_usd=0.02)
    chat(client)
    with pytest.raises(BudgetExceeded, match="process spend cap"):
        chat(client)                                           # a fresh request ledger does not help
    assert len(fake.requests) == 1


def test_no_call_when_deadline_is_nearly_gone():
    client, fake, _ = make_client([(200, completion(), None)])
    with pytest.raises(LLMUnavailable, match="no time left"):
        chat(client, deadline_s=1)
    assert fake.requests == []


def test_retry_sleep_never_runs_past_the_deadline():
    client, fake, sleeps = make_client([(429, {"error": {}}, {"retry-after": "30"}), (200, completion(), None)])
    with pytest.raises(LLMUnavailable):
        chat(client, deadline_s=20)
    assert len(fake.requests) == 1 and sleeps == []


# ------------------------------------------------------------------ key safety
def test_another_providers_key_in_the_environment_is_never_used(monkeypatch, tmp_path):
    """On the default provider, an OpenAI key that happens to be set must not be picked up."""
    monkeypatch.delenv("CODEFIX_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-company-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings.from_env(env_file=None)
    assert settings.llm_provider == "openrouter" and settings.llm_api_key == ""
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY is not set"):
        LLMClient(settings)


def test_a_key_of_the_wrong_shape_is_refused():
    with pytest.raises(ConfigError, match="does not look like a openrouter key"):
        Settings(llm_api_key="sk-proj-abc").validate_llm()


def test_settings_repr_hides_the_key():
    assert KEY not in repr(Settings(llm_api_key=KEY))


def test_env_file_loads_without_overriding(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nOPENROUTER_API_KEY='sk-or-from-file'\nexport CODEFIX_X=1\nCODEFIX_KEEP=file\n")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CODEFIX_X", raising=False)
    monkeypatch.setenv("CODEFIX_KEEP", "process")
    load_env_file(env)
    import os
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-file"
    assert os.environ["CODEFIX_X"] == "1" and os.environ["CODEFIX_KEEP"] == "process"


# ------------------------------------------------------------------ hangs and rotation
def test_a_trickling_provider_cannot_hold_a_call_past_its_timeout():
    """A provider that never finishes (e.g. keep-alive whitespace) is cut off by the wall-clock bound."""
    import asyncio
    import time

    async def hang(request):
        await asyncio.sleep(30)
        return sse_response(completion())
    settings = Settings(llm_api_key=KEY, llm_call_timeout_s=4, llm_max_retries=0)
    client = LLMClient(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(hang)))
    started = time.monotonic()
    with pytest.raises(LLMUnavailable, match="did not answer within"):
        chat(client)
    assert time.monotonic() - started < 6


def test_retries_rotate_the_lead_model():
    client, fake, _ = make_client([(429, {"error": {}}, None), (429, {"error": {}}, None), (200, completion(), None)])
    chat(client, fallback_models=["b/model", "c/model"])
    leads = [b["model"] for b in fake.bodies]
    assert leads == ["openai/gpt-5.6-terra", "b/model", "c/model"]
    assert fake.bodies[1]["models"] == ["b/model", "c/model", "openai/gpt-5.6-terra"]


def test_a_stalled_stream_fails_over_to_the_next_model():
    """First model sends one chunk then goes silent; the stall timer cuts it and the next model answers."""
    import asyncio

    seen = []

    async def handler(request):
        body = json.loads(request.content)
        seen.append(body["model"])
        if body["model"] == "a/slow":
            async def trickle():
                yield sse_chunks(completion(content="partial"))[0]
                await asyncio.sleep(30)
            return httpx.Response(200, content=trickle(), headers={"content-type": "text/event-stream"})
        return sse_response(completion(content="from b"))
    settings = Settings(llm_api_key=KEY, llm_stall_timeout_s=1.0)
    client = LLMClient(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    reply = run(client.chat(model="a/slow", fallback_models=["b/fast"], messages=[{"role": "user", "content": "x"}],
                            deadline=Deadline.after(60), ledger=client.new_request_ledger()))
    assert reply.content == "from b" and seen == ["a/slow", "b/fast"] and reply.attempts == 2


def test_stream_assembles_split_content_and_tool_call_arguments():
    parts = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "lo", "tool_calls": [
            {"index": 0, "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"pa"}}]},
            "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": \"a\"}"}}]},
                      "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7, "cost": 0.001}},
    ]
    events = [{"id": "g", "object": "chat.completion.chunk", "created": 1, "model": "m", **p} for p in parts]
    data = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"
    client = LLMClient(Settings(llm_api_key=KEY), http_client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=data, headers={"content-type": "text/event-stream"}))))
    reply = run(client.chat(model="m", messages=[{"role": "user", "content": "x"}], deadline=Deadline.after(30),
                            ledger=client.new_request_ledger()))
    assert reply.content == "Hello" and reply.finish_reason == "tool_calls"
    assert [(t.id, t.name, t.arguments) for t in reply.tool_calls] == [("c1", "read_file", '{"path": "a"}')]
    assert reply.usage.cost_usd == 0.001


def test_daily_quota_429_stops_without_retrying():
    from app.llm import QuotaExhausted
    msg = {"error": {"message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day"}}
    client, fake, sleeps = make_client([(429, msg, None)])
    with pytest.raises(QuotaExhausted):
        chat(client)
    assert len(fake.requests) == 1 and sleeps == []


def test_a_dropped_connection_mid_stream_is_retried():
    """httpx raises inside the stream iterator; the SDK does not wrap that (regression)."""
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) == 1:
            async def broken():
                yield sse_chunks(completion(content="partial"))[0]
                raise httpx.ReadError("connection reset")
            return httpx.Response(200, content=broken(), headers={"content-type": "text/event-stream"})
        return sse_response(completion(content="recovered"))

    client = LLMClient(Settings(llm_api_key=KEY),
                       http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    reply = run(client.chat(model="m", messages=[{"role": "user", "content": "x"}],
                            deadline=Deadline.after(60), ledger=client.new_request_ledger()))
    assert reply.content == "recovered" and len(calls) == 2
