"""Service settings, read once from the environment (and an optional .env file).

Every limit that shapes behaviour lives here so it can be tuned without code
changes and quoted in the README.

LLM access goes through OpenRouter only. The service reads OPENROUTER_API_KEY
and nothing else: OPENAI_API_KEY is deliberately ignored, so a company OpenAI
key that happens to be in the environment can never be used or charged.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .envfile import load_env_file

SERVICE_ROOT = Path(__file__).resolve().parents[1]
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"

# Which LLM back end the service talks to. All three speak the OpenAI Chat
# Completions wire format; they differ in the key they use, the extra body
# fields they accept and whether they report the cost of a call.
PROVIDERS = {
    # name:        (base URL,            key env var,          key prefix, default models)
    "openrouter": (OPENROUTER_BASE_URL, "OPENROUTER_API_KEY", "sk-or-",
                   ("nex-agi/nex-n2.5-pro:free", "qwen/qwen3.8-27b:free")),
    "openai":     (OPENAI_BASE_URL,     "OPENAI_API_KEY",     "sk-",
                   ("gpt-5.2", "gpt-5-mini")),
    # Any other OpenAI-compatible endpoint (Azure, vLLM, LM Studio, a proxy...).
    "compatible": ("",                  "CODEFIX_LLM_API_KEY", "",
                   ("", "")),
}


class ConfigError(RuntimeError):
    pass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    return tuple(x.strip() for x in raw.split(",") if x.strip()) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def find_bash() -> str:
    """Locate a POSIX bash. On Windows, prefer Git Bash over the WSL launcher."""
    explicit = os.environ.get("CODEFIX_BASH")
    if explicit:
        return explicit
    if os.name == "nt":
        # Git's bin/bash.exe is a launcher that can exit after starting
        # usr/bin/bash.exe.  Use the real shell so a timeout can kill the
        # process tree rooted at the PID we launched.
        for candidate in (r"C:\Program Files\Git\usr\bin\bash.exe",
                          r"C:\Program Files\Git\bin\bash.exe",
                          r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
                          r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(candidate).exists():
                return candidate
    found = shutil.which("bash")
    if not found:
        raise RuntimeError("bash not found; set CODEFIX_BASH")
    return found


@dataclass(frozen=True)
class Settings:
    # Docker mirrors grading exactly and is the only service backend.  Tests
    # instantiate LocalSandbox explicitly; production must never execute a
    # repository on the host.
    sandbox_backend: str = "docker"
    sandbox_image: str = "acceptance:latest"
    work_root: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "codefix")

    # Tool limits (ACI). Sized so one tool result stays well under ~4k tokens.
    read_window_lines: int = 250
    list_dir_max_entries: int = 500
    search_max_hits: int = 50
    search_max_file_bytes: int = 1_000_000
    tool_output_max_chars: int = 16_000
    command_timeout_s: float = 60.0
    test_timeout_s: float = 300.0          # same cap grade.py uses
    min_exec_budget_s: float = 5.0         # refuse to start a run with less time left

    # LLM. Chat Completions in every case; see PROVIDERS.
    llm_provider: str = "openrouter"
    llm_base_url: str = OPENROUTER_BASE_URL
    llm_api_key: str = field(default="", repr=False)
    model_primary: str = "openai/gpt-5.6-terra"
    model_secondary: str = "openai/gpt-5.6-sol"
    model_spec: str = "openai/gpt-5.6-terra"
    model_fallbacks: tuple[str, ...] = ()  # tried by OpenRouter when a model is rate-limited or down
    reasoning_effort: str = "medium"
    llm_call_timeout_s: float = 150.0      # per call; the request deadline usually binds first
    llm_stall_timeout_s: float = 90.0      # cut a streamed call after this long with no output
    llm_max_retries: int = 3
    llm_max_output_tokens: int = 16_000    # bounds the cost of any single call
    provider_data_collection: str = "deny" # never route to providers that train on prompts
    app_title: str = "codefix-service"
    # Used to report cost when the back end does not return one (OpenAI does not).
    # USD per 1M tokens; 0 means "cost unknown", which the response reports honestly.
    price_input_per_m: float = 0.0
    price_cached_input_per_m: float = 0.0
    price_output_per_m: float = 0.0

    # Spend caps, enforced before every call (in addition to OpenRouter's per-key limit).
    max_cost_per_request_usd: float = 1.00
    max_cost_total_usd: float = 5.00       # for the life of the process

    # Pipeline.
    max_candidates: int = 2                # independent attempts, each with its own repairs
    max_repairs: int = 2                   # repair rounds per candidate after a failed check
    spec_checks_enabled: bool = True
    spec_wait_s: float = 30.0              # how long a candidate waits for derived checks before verifying
    fix_max_output_tokens: int = 8_000     # per fix answer; raise for models that reason verbosely
    spec_max_output_tokens: int = 6_000    # per derived-check answer
    sandbox_concurrency: int = max(1, (os.cpu_count() or 2) // 2)
    trace_dir: Path | None = SERVICE_ROOT / "runs"
    # Evaluation mode: give every request at least this many seconds, whatever
    # deadline_seconds says, to measure correctness with slow (free) models.
    # Graded runs keep 0 so the client's deadline is honoured.
    budget_floor_s: float = 0.0

    @property
    def provider_is_openrouter(self) -> bool:
        return self.llm_provider == "openrouter"

    @classmethod
    def from_env(cls, *, env_file: Path | None = SERVICE_ROOT / ".env") -> "Settings":
        if env_file is not None:
            load_env_file(env_file)
        provider = os.environ.get("CODEFIX_LLM_PROVIDER", "openrouter").strip().lower()
        if provider not in PROVIDERS:
            raise ConfigError(f"CODEFIX_LLM_PROVIDER must be one of {', '.join(PROVIDERS)}; got {provider!r}")
        base_url, key_var, _prefix, (default_primary, default_secondary) = PROVIDERS[provider]
        # Only this provider's key variable is read, so a key for another
        # provider that happens to be in the environment is never used.
        api_key = os.environ.get(key_var, "")
        return cls(
            llm_provider=provider,
            sandbox_backend=os.environ.get("CODEFIX_SANDBOX", "docker"),
            sandbox_image=os.environ.get("CODEFIX_SANDBOX_IMAGE", "acceptance:latest"),
            work_root=Path(os.environ.get("CODEFIX_WORK_ROOT",
                                          str(Path(tempfile.gettempdir()) / "codefix"))),
            read_window_lines=_env_int("CODEFIX_READ_WINDOW", 250),
            list_dir_max_entries=_env_int("CODEFIX_LIST_MAX", 500),
            search_max_hits=_env_int("CODEFIX_SEARCH_MAX_HITS", 50),
            tool_output_max_chars=_env_int("CODEFIX_TOOL_OUTPUT_MAX", 16_000),
            command_timeout_s=_env_float("CODEFIX_COMMAND_TIMEOUT", 60.0),
            test_timeout_s=_env_float("CODEFIX_TEST_TIMEOUT", 300.0),
            min_exec_budget_s=_env_float("CODEFIX_MIN_EXEC_BUDGET", 5.0),
            llm_base_url=os.environ.get("CODEFIX_LLM_BASE_URL", base_url),
            llm_api_key=api_key,
            model_primary=os.environ.get("CODEFIX_MODEL_PRIMARY", default_primary),
            model_secondary=os.environ.get("CODEFIX_MODEL_SECONDARY", default_secondary),
            model_spec=os.environ.get("CODEFIX_MODEL_SPEC", default_primary),
            model_fallbacks=_env_list("CODEFIX_MODEL_FALLBACKS", ()),
            reasoning_effort=os.environ.get("CODEFIX_REASONING_EFFORT", "medium"),
            llm_call_timeout_s=_env_float("CODEFIX_LLM_CALL_TIMEOUT", 150.0),
            llm_stall_timeout_s=_env_float("CODEFIX_LLM_STALL_TIMEOUT", 90.0),
            llm_max_retries=_env_int("CODEFIX_LLM_MAX_RETRIES", 3),
            llm_max_output_tokens=_env_int("CODEFIX_LLM_MAX_OUTPUT_TOKENS", 16_000),
            provider_data_collection=os.environ.get("CODEFIX_PROVIDER_DATA_COLLECTION", "deny"),
            max_cost_per_request_usd=_env_float("CODEFIX_MAX_COST_PER_REQUEST_USD", 1.00),
            max_cost_total_usd=_env_float("CODEFIX_MAX_COST_TOTAL_USD", 5.00),
            price_input_per_m=_env_float("CODEFIX_PRICE_INPUT_PER_M", 0.0),
            price_cached_input_per_m=_env_float("CODEFIX_PRICE_CACHED_INPUT_PER_M", 0.0),
            price_output_per_m=_env_float("CODEFIX_PRICE_OUTPUT_PER_M", 0.0),
            max_candidates=_env_int("CODEFIX_MAX_CANDIDATES", 2),
            max_repairs=_env_int("CODEFIX_MAX_REPAIRS", 2),
            spec_checks_enabled=os.environ.get("CODEFIX_SPEC_CHECKS", "1") != "0",
            spec_wait_s=_env_float("CODEFIX_SPEC_WAIT_S", 30.0),
            fix_max_output_tokens=_env_int("CODEFIX_FIX_MAX_OUTPUT_TOKENS", 8_000),
            spec_max_output_tokens=_env_int("CODEFIX_SPEC_MAX_OUTPUT_TOKENS", 6_000),
            sandbox_concurrency=_env_int("CODEFIX_SANDBOX_CONCURRENCY", max(1, (os.cpu_count() or 2) // 2)),
            trace_dir=Path(os.environ["CODEFIX_TRACE_DIR"]) if os.environ.get("CODEFIX_TRACE_DIR") else SERVICE_ROOT / "runs",
            budget_floor_s=_env_float("CODEFIX_BUDGET_FLOOR_S", 0.0),
        )

    def validate_llm(self) -> None:
        """Refuse to start LLM work misconfigured: no key, the wrong kind of key, or no endpoint."""
        if self.llm_provider not in PROVIDERS:
            raise ConfigError(f"unknown CODEFIX_LLM_PROVIDER {self.llm_provider!r}")
        _base, key_var, prefix, _models = PROVIDERS[self.llm_provider]
        others = ", ".join(v for k, (_b, v, _p, _m) in PROVIDERS.items() if k != self.llm_provider)
        if not self.llm_api_key:
            raise ConfigError(f"{key_var} is not set for CODEFIX_LLM_PROVIDER={self.llm_provider} "
                              f"(keys for other providers — {others} — are deliberately ignored)")
        if prefix and not self.llm_api_key.startswith(prefix):
            raise ConfigError(f"{key_var} does not look like a {self.llm_provider} key "
                              f"(expected it to start with {prefix!r}); refusing to send it anywhere")
        if self.llm_provider == "openrouter" and self.llm_api_key.startswith("sk-proj-"):
            raise ConfigError("that looks like an OpenAI key, not an OpenRouter key")
        if not self.llm_base_url:
            raise ConfigError("CODEFIX_LLM_BASE_URL must be set for CODEFIX_LLM_PROVIDER=compatible")
        if not self.model_primary:
            raise ConfigError("CODEFIX_MODEL_PRIMARY must be set")
        if self.max_cost_per_request_usd <= 0 or self.max_cost_total_usd <= 0:
            raise ConfigError("cost caps must be positive")
        if not self.spec_checks_enabled:
            raise ConfigError("CODEFIX_SPEC_CHECKS cannot be disabled: the request contract requires derived checks")
