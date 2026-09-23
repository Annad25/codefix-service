"""Check the OpenRouter key safely before any real run.

    python scripts/check_openrouter.py          # key status only: costs nothing
    python scripts/check_openrouter.py --ping   # plus one tiny call to a free model ($0)
    python scripts/check_openrouter.py --ping --model openai/gpt-5.6-luna   # tiny paid call (~$0.0001)

Reads OPENROUTER_API_KEY from the environment or codefix-service/.env.
OPENAI_API_KEY is never read.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ConfigError, Settings  # noqa: E402
from app.deadline import Deadline  # noqa: E402
from app.llm import LLMClient, LLMError  # noqa: E402

FREE_MODEL = "qwen/qwen3.8-27b:free"


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ping", action="store_true", help="send one tiny chat request")
    p.add_argument("--model", default=FREE_MODEL, help=f"model for --ping (default {FREE_MODEL}, free)")
    a = p.parse_args()

    settings = Settings.from_env()
    try:
        client = LLMClient(settings)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    key = settings.llm_api_key
    print(f"key: {key[:9]}...{key[-4:]}  base_url: {settings.llm_base_url}")
    print(f"service caps: ${settings.max_cost_per_request_usd:.2f}/request, "
          f"${settings.max_cost_total_usd:.2f}/process")

    status = await client.key_status()
    limit = status.get("limit")
    print(f"OpenRouter key limit: {'NONE SET' if limit is None else f'${limit:.2f}'}"
          f"  remaining: {status.get('limit_remaining')}  used so far: ${status.get('usage', 0):.4f}"
          f"  free tier: {status.get('is_free_tier')}")
    if limit is None:
        print("WARNING: this key has no credit limit. Set one at https://openrouter.ai/settings/keys")

    if a.ping:
        ledger = client.new_request_ledger()
        try:
            reply = await client.chat(model=a.model, deadline=Deadline.after(60), ledger=ledger,
                                      messages=[{"role": "user", "content": "Reply with the word: pong"}],
                                      reasoning_effort="", max_output_tokens=200)
        except LLMError as exc:
            print(f"ping failed: {exc}")
            return 1
        print(f"ping via {reply.model}: {reply.content!r}  cost=${reply.usage.cost_usd:.6f}")
    await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
