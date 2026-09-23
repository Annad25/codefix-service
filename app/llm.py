"""LLM client: Chat Completions, against OpenRouter, OpenAI or any compatible endpoint.

The back end is chosen by CODEFIX_LLM_PROVIDER, so a reviewer can run the
service on their own key without touching the code. Only the selected
provider's key variable is ever read.

Responsibilities:
  - one place that talks to the model provider (provider, base URL and key are config)
  - per-provider request dialect: OpenRouter routing/cost options and max_tokens,
    OpenAI max_completion_tokens + top-level reasoning_effort, or the portable subset
  - spend control: a per-request and a process-wide CostLedger are checked
    before every call, and every call's output is capped (max_tokens), so the
    worst-case overshoot of a cap is one bounded call. OpenRouter's own per-key
    credit limit sits underneath as a hard stop (HTTP 402).
  - cost accounting: exact from OpenRouter's usage.cost, or computed from the
    configured per-1M prices when the back end reports none (OpenAI), or honestly
    reported as unknown
  - streaming, with a stall timer: a slow model that keeps producing tokens runs
    to the deadline, a silent one is cut off and retried on the next model
  - retries only for transient failures (timeouts, stalls, 408/409/429/5xx, empty
    responses), with exponential backoff + jitter, honouring Retry-After, and
    never past the request deadline
  - privacy routing: provider.data_collection = "deny"; require_parameters so
    requests only go to providers that support tool calling
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

from .config import Settings
from .deadline import Deadline

log = logging.getLogger("codefix.llm")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
MIN_CALL_SECONDS = 3.0


class LLMError(Exception):
    """Base class: the call did not produce a usable reply."""


class BudgetExceeded(LLMError):
    """A spend cap was reached (ours, or OpenRouter's key limit / credits: HTTP 402)."""


class LLMUnavailable(LLMError):
    """Non-retryable failure, retries exhausted, or no time left."""


class QuotaExhausted(BudgetExceeded):
    """The account's daily request quota is used up (e.g. OpenRouter free-models-per-day)."""


def _is_daily_quota(exc: Exception) -> bool:
    text = str(exc).lower()
    return "per-day" in text or "per day" in text or "daily limit" in text


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True        # False if any call came back without a cost figure

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cost_usd += other.cost_usd
        self.cost_known = self.cost_known and other.cost_known

    def to_dict(self) -> dict[str, Any]:
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "cached_tokens": self.cached_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "estimated_cost_usd": round(self.cost_usd, 6), "cost_known": self.cost_known}


class CostLedger:
    """Accumulates usage and refuses new calls once its cap is reached."""

    def __init__(self, name: str, cap_usd: float | None) -> None:
        self.name = name
        self.cap_usd = cap_usd
        self.usage = Usage()

    @property
    def spent_usd(self) -> float:
        return self.usage.cost_usd

    def check(self) -> None:
        if self.cap_usd is not None and self.usage.cost_usd >= self.cap_usd:
            raise BudgetExceeded(f"{self.name} spend cap of ${self.cap_usd:.2f} reached "
                                 f"(spent ${self.usage.cost_usd:.4f})")

    def charge(self, usage: Usage) -> None:
        self.usage.add(usage)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str          # raw JSON string, validated by the tool dispatcher


@dataclass
class LLMReply:
    model: str
    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: Usage
    message: dict[str, Any]              # assistant message to append to the conversation
    reasoning: str | None = None
    attempts: int = 1


def _extra(obj: Any, name: str) -> Any:
    """Read a provider-specific field the OpenAI SDK keeps in model_extra."""
    if obj is None:
        return None
    value = getattr(obj, name, None)
    if value is None and getattr(obj, "model_extra", None):
        value = obj.model_extra.get(name)
    return value


def parse_usage(raw: Any, prices: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> Usage:
    """Token counts, plus cost: exact when the back end reports one (OpenRouter),
    otherwise computed from the configured per-1M prices, otherwise unknown."""
    if raw is None:
        return Usage(calls=1, cost_known=False)
    prompt_details = getattr(raw, "prompt_tokens_details", None)
    completion_details = getattr(raw, "completion_tokens_details", None)
    prompt = raw.prompt_tokens or 0
    completion = raw.completion_tokens or 0
    cached = (getattr(prompt_details, "cached_tokens", None) or 0) if prompt_details else 0
    reasoning = (getattr(completion_details, "reasoning_tokens", None) or 0) if completion_details else 0

    reported = _extra(raw, "cost")
    if reported is not None:
        cost, known = float(reported), True
    else:
        price_in, price_cached, price_out = prices
        if price_in or price_out:
            cost = ((prompt - cached) * price_in + cached * price_cached + completion * price_out) / 1e6
            known = True
        else:
            cost, known = 0.0, False
    return Usage(calls=1, prompt_tokens=prompt, completion_tokens=completion, cached_tokens=cached,
                 reasoning_tokens=reasoning, cost_usd=cost, cost_known=known)


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    for key in ("retry-after-ms", "retry-after"):
        value = headers.get(key)
        if value:
            try:
                seconds = float(value)
                return seconds / 1000 if key.endswith("-ms") else seconds
            except ValueError:
                return None
    return None


class LLMClient:
    def __init__(self, settings: Settings, *, global_ledger: CostLedger | None = None,
                 http_client: httpx.AsyncClient | None = None, sleep=asyncio.sleep) -> None:
        settings.validate_llm()
        self.settings = settings
        self.global_ledger = global_ledger or CostLedger("process", settings.max_cost_total_usd)
        self._sleep = sleep
        self._prices = (settings.price_input_per_m, settings.price_cached_input_per_m,
                        settings.price_output_per_m)
        # api_key and base_url are always passed explicitly, so the SDK never
        # falls back to OPENAI_API_KEY / OPENAI_BASE_URL from the environment.
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key, base_url=settings.llm_base_url,
            max_retries=0, timeout=settings.llm_call_timeout_s, http_client=http_client,
            # App attribution headers are an OpenRouter convention; other back ends ignore
            # them, but there is no reason to send them anywhere else.
            default_headers={"HTTP-Referer": "https://github.com/codefix-service",
                             "X-Title": settings.app_title} if settings.provider_is_openrouter else None,
        )

    def new_request_ledger(self) -> CostLedger:
        return CostLedger("request", self.settings.max_cost_per_request_usd)

    def _request_kwargs(self, model: str, messages: list[dict[str, Any]], effort: str | None,
                        max_output_tokens: int) -> dict[str, Any]:
        """Body for one call, in the dialect the configured back end accepts.

        OpenRouter takes its routing and cost options in extra_body and uses
        max_tokens. OpenAI rejects those fields, wants max_completion_tokens
        (max_tokens is deprecated and invalid for reasoning models) and takes
        reasoning_effort at the top level. A generic compatible endpoint gets
        the plain, portable subset.
        """
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if self.settings.llm_provider == "openrouter":
            kwargs["max_tokens"] = max_output_tokens
            kwargs["extra_body"] = {
                "provider": {"data_collection": self.settings.provider_data_collection,
                             "require_parameters": True, "allow_fallbacks": True},
                "usage": {"include": True},        # ask for the exact cost of the call
            }
            if effort:
                kwargs["extra_body"]["reasoning"] = {"effort": effort}
        elif self.settings.llm_provider == "openai":
            kwargs["max_completion_tokens"] = max_output_tokens
            if effort:
                kwargs["reasoning_effort"] = effort
        else:                                       # compatible: portable subset only
            kwargs["max_tokens"] = max_output_tokens
        return kwargs

    async def chat(self, *, model: str, messages: list[dict[str, Any]], deadline: Deadline,
                   ledger: CostLedger, tools: list[dict[str, Any]] | None = None,
                   tool_choice: str | dict | None = None, reasoning_effort: str | None = None,
                   response_format: dict[str, Any] | None = None,
                   max_output_tokens: int | None = None,
                   fallback_models: list[str] | None = None) -> LLMReply:
        effort = self.settings.reasoning_effort if reasoning_effort is None else reasoning_effort
        kwargs = self._request_kwargs(model, messages, effort or None,
                                      max_output_tokens or self.settings.llm_max_output_tokens)
        # Model fallbacks are an OpenRouter feature; elsewhere the chain is rotated client-side.
        chain = [model, *[m for m in (fallback_models or []) if m != model]]
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if response_format:
            kwargs["response_format"] = response_format

        last_error: Exception | None = None
        for attempt in range(1, self.settings.llm_max_retries + 2):
            ledger.check()
            self.global_ledger.check()
            timeout = deadline.timeout(self.settings.llm_call_timeout_s)
            if timeout < MIN_CALL_SECONDS:
                raise LLMUnavailable(f"no time left for an LLM call ({timeout:.1f}s)") from last_error
            # Each retry leads with the next model in the chain; OpenRouter falls back
            # through the rest server-side (rate limits, downtime, context errors).
            lead = (attempt - 1) % len(chain)
            order = chain[lead:] + chain[:lead]
            kwargs["model"] = order[0]
            if len(order) > 1 and self.settings.llm_provider == "openrouter":
                kwargs["extra_body"]["models"] = order
            try:
                # Streamed, so a slow-but-working model keeps going while a silent one is
                # cut off by the stall timer; wait_for bounds the whole call regardless of
                # keep-alive bytes a provider may send.
                reply = await asyncio.wait_for(self._stream(kwargs, timeout, attempt), timeout=timeout)
            except openai.APIStatusError as exc:
                if exc.status_code == 402:
                    raise BudgetExceeded("OpenRouter refused the call: credits or key limit exhausted "
                                         f"(402): {exc.message}") from exc
                if exc.status_code == 429 and _is_daily_quota(exc):
                    raise QuotaExhausted(f"daily request quota exhausted: {exc.message}") from exc
                if exc.status_code not in RETRYABLE_STATUS:
                    raise LLMUnavailable(f"LLM call failed ({exc.status_code}): {exc.message}") from exc
                last_error = exc
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"{kwargs['model']} did not answer within {timeout:.0f}s")
            except (openai.APITimeoutError, openai.APIConnectionError, openai.APIError,
                    # httpx errors raised while iterating the stream are not wrapped by the
                    # SDK (it only wraps the request); a dropped connection is retryable.
                    httpx.HTTPError, _EmptyResponse, _Stalled) as exc:
                last_error = exc if str(exc) else type(exc).__name__
            else:
                ledger.charge(reply.usage)
                self.global_ledger.charge(reply.usage)
                log.info("llm model=%s attempt=%d prompt=%d cached=%d out=%d cost=$%.5f",
                         reply.model, attempt, reply.usage.prompt_tokens, reply.usage.cached_tokens,
                         reply.usage.completion_tokens, reply.usage.cost_usd)
                return reply

            if attempt > self.settings.llm_max_retries:
                break
            delay = _retry_after_seconds(last_error)
            if delay is None:
                delay = min(8.0, 0.5 * 2 ** (attempt - 1)) * random.uniform(0.5, 1.0)
            if deadline.remaining() - delay < MIN_CALL_SECONDS:
                break
            log.warning("llm retry %d after %.1fs: %s", attempt, delay, last_error)
            await self._sleep(delay)
        raise LLMUnavailable(f"LLM call failed after retries: {last_error}") from last_error

    async def _stream(self, kwargs: dict[str, Any], timeout: float, attempts: int) -> LLMReply:
        """One streamed completion, assembled into an LLMReply."""
        stall = self.settings.llm_stall_timeout_s
        stream = await self._client.chat.completions.create(
            stream=True, stream_options={"include_usage": True}, timeout=timeout, **kwargs)
        content: list[str] = []
        reasoning: list[str] = []
        reasoning_details: list[Any] = []
        tools: dict[int, dict[str, Any]] = {}
        finish, usage_raw, model, saw_choice = None, None, kwargs["model"], False
        chunks = stream.__aiter__()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(chunks.__anext__(), timeout=stall)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise _Stalled(f"{model} produced no output for {stall:.0f}s") from None
                model = chunk.model or model
                if chunk.usage is not None:
                    usage_raw = chunk.usage
                for choice in chunk.choices or []:
                    saw_choice = True
                    delta = choice.delta
                    if delta is not None:
                        if delta.content:
                            content.append(delta.content)
                        text = _extra(delta, "reasoning")
                        if text:
                            reasoning.append(text)
                        details = _extra(delta, "reasoning_details")
                        if details:
                            reasoning_details.extend(details)
                        for tc in delta.tool_calls or []:
                            slot = tools.setdefault(tc.index, {"id": "", "name": "", "arguments": []})
                            slot["id"] = tc.id or slot["id"]
                            if tc.function is not None:
                                slot["name"] = slot["name"] or (tc.function.name or "")
                                if tc.function.arguments:
                                    slot["arguments"].append(tc.function.arguments)
                    if choice.finish_reason:
                        finish = choice.finish_reason
        finally:
            await stream.close()
        if not saw_choice:
            raise _EmptyResponse("provider returned no choices")
        if finish == "error":
            raise _EmptyResponse("provider reported an error mid-stream")
        text = "".join(content) or None
        tool_calls = [ToolCall(id=s["id"], name=s["name"], arguments="".join(s["arguments"]))
                      for _, s in sorted(tools.items()) if s["name"]]
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            message["tool_calls"] = [{"id": tc.id, "type": "function",
                                      "function": {"name": tc.name, "arguments": tc.arguments}}
                                     for tc in tool_calls]
        # Pass reasoning back on later turns, as OpenRouter recommends for tool calling.
        if reasoning_details:
            message["reasoning_details"] = reasoning_details
        return LLMReply(model=model, content=text, tool_calls=tool_calls, finish_reason=finish,
                        usage=parse_usage(usage_raw, self._prices), message=message,
                        reasoning="".join(reasoning) or None, attempts=attempts)

    async def key_status(self) -> dict[str, Any]:
        """GET /key: remaining limit and usage for this key (OpenRouter only). Costs nothing."""
        if not self.settings.provider_is_openrouter:
            return {"note": f"key status is only available for OpenRouter "
                            f"(provider is {self.settings.llm_provider})"}
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(f"{self.settings.llm_base_url}/key",
                               headers={"Authorization": f"Bearer {self.settings.llm_api_key}"})
            r.raise_for_status()
            return r.json().get("data", {})

    async def aclose(self) -> None:
        await self._client.close()


class _EmptyResponse(Exception):
    pass


class _Stalled(Exception):
    pass
