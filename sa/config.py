"""Settings from the environment (and `.env`): provider, models, the vault root, budgets.

`load()` reads the environment every time it is called, so tests and evals can point SA_ROOT at a temp dir.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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
    root: Path  # the vault
    state: Path  # sessions, response cache (.sa/)
    tz: str
    max_steps: int  # per task
    max_cost: float  # USD per task
    max_tokens: int  # prompt + completion tokens per task: the budget that works on free models too
    max_result_chars: int
    keep_messages: int  # raw messages of recent tasks kept in context
    keep_summaries: int  # rolling summaries of finished tasks kept in context
    cache: bool
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


def load() -> Config:
    provider = _get("SA_PROVIDER", "openrouter")
    if provider == "openrouter":
        base_url, api_key, defaults = OPENROUTER_URL, _get("OPENROUTER_API_KEY"), OPENROUTER_DEFAULTS
    elif provider == "custom":  # llama.cpp, vLLM, LM Studio: any OpenAI-compatible server
        base_url, api_key = _get("SA_BASE_URL", "http://localhost:8080/v1"), _get("SA_API_KEY", "none")
        defaults = {"model": "", "small_model": "", "fallbacks": ""}
    else:
        raise ValueError(f"SA_PROVIDER must be 'openrouter' or 'custom', got {provider!r}")

    model = _get("SA_MODEL", defaults["model"])
    return Config(
        provider=provider,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
        small_model=_get("SA_SMALL_MODEL", defaults["small_model"] or model),
        fallbacks=tuple(m.strip() for m in _get("SA_FALLBACKS", defaults["fallbacks"]).split(",") if m.strip()),
        root=Path(_get("SA_ROOT", str(DEFAULT_ROOT))).expanduser().resolve(),
        state=Path(_get("SA_STATE", str(PROJECT / ".sa"))).expanduser().resolve(),
        tz=_get("SA_TZ", "Asia/Riyadh"),
        max_steps=_number("SA_MAX_STEPS", "20", int),
        max_cost=_number("SA_MAX_COST_USD", "0.05", float),
        max_tokens=_number("SA_MAX_TOKENS", "150000", int),
        max_result_chars=_number("SA_MAX_RESULT_CHARS", "4000", int),
        keep_messages=_number("SA_KEEP_MESSAGES", "20", int),
        keep_summaries=_number("SA_KEEP_SUMMARIES", "10", int),
        cache=_get("SA_CACHE", "0") == "1",
        timeout=_number("SA_TIMEOUT", "120", float),
        retries=_number("SA_RETRIES", "2", int),
    )
