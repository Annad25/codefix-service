"""CODEFIX_LLM_PROVIDER: the same pipeline against OpenRouter, OpenAI or any compatible endpoint.

Each back end takes a different request dialect, and only the selected
provider's key variable may ever be read.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import ConfigError, Settings
from app.deadline import Deadline
from app.llm import LLMClient

from conftest import run, sse_response

COMPLETION = {
    "id": "g", "object": "chat.completion", "created": 1, "model": "m",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
              "prompt_tokens_details": {"cached_tokens": 800}},
}


def call(settings, **chat_kwargs):
    """One chat call against a fake endpoint; returns (reply, request body, request)."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        seen["body"] = json.loads(request.content)
        return sse_response(COMPLETION)

    client = LLMClient(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    reply = run(client.chat(model="m", messages=[{"role": "user", "content": "hi"}],
                            deadline=Deadline.after(60), ledger=client.new_request_ledger(),
                            reasoning_effort="low", **chat_kwargs))
    return reply, seen["body"], seen["request"]


# ------------------------------------------------------------------ request dialects
def test_openrouter_sends_routing_cost_and_max_tokens():
    settings = Settings(llm_provider="openrouter", llm_api_key="sk-or-v1-x")
    _, body, request = call(settings, fallback_models=["b/model"])
    assert body["max_tokens"] == 16_000 and "max_completion_tokens" not in body
    assert body["reasoning"] == {"effort": "low"} and "reasoning_effort" not in body
    assert body["usage"] == {"include": True}
    assert body["provider"]["data_collection"] == "deny"
    assert body["models"] == ["m", "b/model"]              # server-side fallbacks
    assert request.headers["x-title"] == "codefix-service"
    assert str(request.url).startswith("https://openrouter.ai/api/v1")


def test_openai_sends_max_completion_tokens_and_top_level_reasoning_effort():
    """OpenAI rejects OpenRouter's fields, and max_tokens is invalid for reasoning models."""
    settings = Settings(llm_provider="openai", llm_base_url="https://api.openai.com/v1",
                        llm_api_key="sk-test-key")
    _, body, request = call(settings, fallback_models=["b/model"])
    assert body["max_completion_tokens"] == 16_000 and "max_tokens" not in body
    assert body["reasoning_effort"] == "low" and "reasoning" not in body
    for openrouter_only in ("provider", "usage", "models"):
        assert openrouter_only not in body
    assert "x-title" not in request.headers
    assert str(request.url).startswith("https://api.openai.com/v1")
    assert request.headers["authorization"] == "Bearer sk-test-key"


def test_compatible_endpoint_sends_only_the_portable_subset():
    settings = Settings(llm_provider="compatible", llm_base_url="http://localhost:1234/v1",
                        llm_api_key="anything")
    _, body, request = call(settings)
    assert body["max_tokens"] == 16_000
    for skipped in ("provider", "usage", "models", "reasoning", "reasoning_effort"):
        assert skipped not in body
    assert str(request.url).startswith("http://localhost:1234/v1")


def test_streaming_and_tools_are_provider_independent():
    for settings in (Settings(llm_provider="openrouter", llm_api_key="sk-or-v1-x"),
                     Settings(llm_provider="openai", llm_api_key="sk-x")):
        _, body, _ = call(settings, tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}])
        assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
        assert body["tools"][0]["function"]["name"] == "t"


# ------------------------------------------------------------------ cost reporting
def test_cost_is_exact_when_the_back_end_reports_it():
    settings = Settings(llm_provider="openrouter", llm_api_key="sk-or-v1-x")
    seen = {}

    def handler(request):
        seen["ok"] = True
        return sse_response({**COMPLETION, "usage": {**COMPLETION["usage"], "cost": 0.004}})
    client = LLMClient(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    reply = run(client.chat(model="m", messages=[{"role": "user", "content": "x"}],
                            deadline=Deadline.after(30), ledger=client.new_request_ledger()))
    assert reply.usage.cost_usd == pytest.approx(0.004) and reply.usage.cost_known


def test_cost_is_computed_from_configured_prices_when_none_is_reported():
    # 200 uncached input @ $1.75/M + 800 cached @ $0.175/M + 500 output @ $14/M
    settings = Settings(llm_provider="openai", llm_api_key="sk-x", price_input_per_m=1.75,
                        price_cached_input_per_m=0.175, price_output_per_m=14.0)
    reply, _, _ = call(settings)
    expected = (200 * 1.75 + 800 * 0.175 + 500 * 14.0) / 1e6
    assert reply.usage.cost_usd == pytest.approx(expected) and reply.usage.cost_known
    assert reply.usage.cached_tokens == 800


def test_cost_is_reported_as_unknown_rather_than_zero():
    settings = Settings(llm_provider="openai", llm_api_key="sk-x")
    reply, _, _ = call(settings)
    assert reply.usage.cost_usd == 0.0 and reply.usage.cost_known is False
    assert reply.usage.to_dict()["cost_known"] is False        # the response says so honestly


# ------------------------------------------------------------------ key handling
def test_each_provider_reads_only_its_own_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-router")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("CODEFIX_LLM_API_KEY", "compat-key")

    monkeypatch.setenv("CODEFIX_LLM_PROVIDER", "openrouter")
    assert Settings.from_env(env_file=None).llm_api_key == "sk-or-v1-router"
    monkeypatch.setenv("CODEFIX_LLM_PROVIDER", "openai")
    openai_settings = Settings.from_env(env_file=None)
    assert openai_settings.llm_api_key == "sk-openai"
    assert openai_settings.llm_base_url == "https://api.openai.com/v1"
    assert openai_settings.model_primary == "gpt-5.2"
    monkeypatch.setenv("CODEFIX_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("CODEFIX_LLM_BASE_URL", "http://localhost:8080/v1")
    assert Settings.from_env(env_file=None).llm_api_key == "compat-key"


def test_missing_or_mismatched_keys_are_refused(monkeypatch):
    with pytest.raises(ConfigError, match="OPENAI_API_KEY is not set"):
        Settings(llm_provider="openai", llm_api_key="").validate_llm()
    with pytest.raises(ConfigError, match="does not look like a openrouter key"):
        Settings(llm_provider="openrouter", llm_api_key="sk-openai-style").validate_llm()
    with pytest.raises(ConfigError, match="CODEFIX_LLM_BASE_URL must be set"):
        Settings(llm_provider="compatible", llm_api_key="k", llm_base_url="").validate_llm()
    monkeypatch.setenv("CODEFIX_LLM_PROVIDER", "anthropic")
    with pytest.raises(ConfigError, match="must be one of"):
        Settings.from_env(env_file=None)


def test_key_status_is_openrouter_only():
    client = LLMClient(Settings(llm_provider="openai", llm_api_key="sk-x"))
    assert "only available for OpenRouter" in run(client.key_status())["note"]
