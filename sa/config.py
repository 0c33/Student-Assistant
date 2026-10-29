"""Settings from the environment (and `.env`): provider, models, the vault root, budgets.

`load()` reads the environment every time it is called, so tests and evals can point SA_ROOT at a temp dir.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT / ".env")  # never overrides variables that are already set

DEFAULT_ROOT = PROJECT / "vault"  # used when SA_ROOT is not set; the only vault main.py creates by itself
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULTS = {
    "model": "google/gemma-4-31b-it:free",
    "small_model": "google/gemma-4-26b-a4b-it:free",
    "fallbacks": "google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3-super-120b-a12b:free",
}


@dataclass(frozen=True)
class Config:
    provider: str  # openrouter | custom
    base_url: str
    api_key: str
    model: str
    small_model: str  # summaries (and later the LLM judge)
    fallbacks: tuple[str, ...]  # tried in order when a model is rate-limited, down or missing
    backup: "Config | None"  # SA_FALLBACK_PROVIDER: a second provider tried when every model above failed
    root: Path  # the vault
    state: Path  # sessions, response cache (.sa/)
    tz: str
    max_steps: int  # per task
    max_cost: float  # USD per task
    max_tokens: int  # prompt + completion tokens per task: the budget that works on free models too
    max_result_chars: int  # a longer tool result is cut, with a note saying how much
    context_tokens: int  # budget for one whole request (system prompt + history), estimated pessimistically
    keep_summaries: int  # rolling summaries of finished tasks kept in context
    memory: str  # inject (MEMORY.MD in the system prompt) | tool (the model reads it with read_file)
    memory_cap: int  # characters: a write that would make MEMORY.MD longer is refused
    tools: str  # local (in-process) | mcp (from servers/vault_mcp.py over stdio)
    cache: bool  # SA_CACHE=1 (record and replay) or SA_CACHE=replay (replay only: a miss is an error, no network)
    replay: bool
    cache_dir: Path
    cache_salt: str  # part of every cache key: evals set one per trial, so trials are separate samples
    today: str  # YYYY-MM-DD to pin "today" (evals); empty = the real date
    decision_url: str  # Sami's local decision model (sa/decide.py); empty = no clarify gate
    decision_threshold: float  # p(ask_more) from which the agent is told to ask first
    timeout: float  # seconds per request
    retries: int  # per model, on 429 / 5xx / network errors


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _number(name: str, default: str, kind: type):
    raw = _get(name, default)
    try:
        return kind(raw)
    except ValueError:
        raise ValueError(f"{name} must be a {kind.__name__}, got {raw!r}") from None


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _get(name, default)
    if value not in allowed:
        raise ValueError(f"{name} must be one of {allowed}, got {value!r}")
    return value


def load() -> Config:
    cfg = _load_provider(_get("SA_PROVIDER", "openrouter"), "SA_")
    backup = _get("SA_FALLBACK_PROVIDER")
    if not backup:
        return cfg
    # the backup provider: same settings, its own endpoint, key and model (SA_FALLBACK_BASE_URL / _API_KEY / _MODEL)
    alt = _load_provider(backup, "SA_FALLBACK_")
    return replace(cfg, backup=replace(alt, fallbacks=(), backup=None))


def _load_provider(provider: str, prefix: str) -> Config:
    """The settings, with the provider's endpoint, key and model read from `prefix`-named variables."""
    if provider == "openrouter":
        base_url, api_key, defaults = OPENROUTER_URL, _get("OPENROUTER_API_KEY"), OPENROUTER_DEFAULTS
    elif provider == "custom":  # llama.cpp, vLLM, LM Studio: any OpenAI-compatible server
        base_url, api_key = _get(f"{prefix}BASE_URL", "http://localhost:8080/v1"), _get(f"{prefix}API_KEY", "none")
        defaults = {"model": "", "small_model": "", "fallbacks": ""}
    else:
        raise ValueError(f"SA_PROVIDER must be 'openrouter' or 'custom', got {provider!r}")

    model = _get(f"{prefix}MODEL", defaults["model"])
    return Config(
        provider=provider,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        small_model=_get("SA_SMALL_MODEL", defaults["small_model"] or model),
        fallbacks=tuple(m.strip() for m in _get("SA_FALLBACKS", defaults["fallbacks"]).split(",") if m.strip()),
        backup=None,
        root=Path(_get("SA_ROOT", str(DEFAULT_ROOT))).expanduser().resolve(),
        state=Path(_get("SA_STATE", str(PROJECT / ".sa"))).expanduser().resolve(),
        tz=_get("SA_TZ", "Asia/Riyadh"),
        max_steps=_number("SA_MAX_STEPS", "20", int),
        max_cost=_number("SA_MAX_COST_USD", "0.05", float),
        max_tokens=_number("SA_MAX_TOKENS", "150000", int),
        max_result_chars=_number("SA_MAX_RESULT_CHARS", "4000", int),
        context_tokens=_number("SA_CONTEXT_TOKENS", "24000", int),
        keep_summaries=_number("SA_KEEP_SUMMARIES", "10", int),
        memory=_choice("SA_MEMORY", "inject", ("inject", "tool")),
        memory_cap=_number("SA_MEMORY_CAP", "2200", int),
        tools=_choice("SA_TOOLS", "local", ("local", "mcp")),
        cache=_get("SA_CACHE", "0") in ("1", "replay"),
        replay=_get("SA_CACHE", "0") == "replay",
        cache_dir=Path(_get("SA_CACHE_DIR", str(Path(_get("SA_STATE", str(PROJECT / ".sa"))) / "cache"))).expanduser(),
        cache_salt=_get("SA_CACHE_SALT"),
        today=_get("SA_TODAY"),
        decision_url=_get("SA_DECISION_URL"),
        decision_threshold=_number("SA_DECISION_THRESHOLD", "0.6", float),
        timeout=_number("SA_TIMEOUT", "120", float),
        retries=_number("SA_RETRIES", "2", int),
    )
