"""Talks to the model: one `requests.post` to `/chat/completions`, the same call the first `main.py` made.

What it adds to that call:
- `model` in every request (vLLM and OpenRouter require it) and `timeout=` (a hung server can't freeze the agent);
- retries with backoff on 429 / 5xx / network errors, then the next fallback model, in order;
- a ledger of prompt/completion tokens and cost;
- an optional disk cache (SA_CACHE=1) that replays identical requests, to save free-model quota;
- a typed `LLMError` instead of returning an "LLM Error: ..." string the caller can't use.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests

from sa import config as sa_config
from sa.config import Config

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}  # temporary: wait, then try the same model again
NEXT_MODEL_STATUS = {404}  # this model is missing or has no endpoint: try the next model
MAX_WAIT = 30.0  # seconds; a Retry-After of hours (daily quota gone) is not worth waiting for

_sleep = time.sleep  # tests replace this


class LLMError(RuntimeError):
    """The model could not answer (after retries and fallbacks), or the request itself was refused."""


@dataclass
class Ledger:
    """Tokens and cost of every request made by this process."""

    calls: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0  # USD, as reported by the provider (free models: 0)

    def add(self, usage: dict | None, cached: bool = False) -> None:
        usage = usage or {}
        self.calls += 1
        self.cache_hits += cached
        self.prompt_tokens += usage.get("prompt_tokens") or 0
        self.completion_tokens += usage.get("completion_tokens") or 0
        if not cached:
            self.cost += float(usage.get("cost") or 0)

    def __str__(self) -> str:
        return (f"{self.calls} model calls ({self.cache_hits} from cache) · {self.prompt_tokens:,} prompt"
                f" + {self.completion_tokens:,} completion tokens · ${self.cost:.4f}")


LEDGER = Ledger()


def chat(messages: list[dict], tools: list[dict] | None = None, model: str | None = None, *,
         cfg: Config | None = None) -> dict:
    """Send the conversation; return the assistant message as a plain dict:
    `{"role": "assistant", "content": str | None, "tool_calls": [...]}` ("tool_calls" only when the model made some).

    Tries `model` (default SA_MODEL), then each SA_FALLBACKS model. Raises `LLMError` when none of them answers.
    """
    cfg = cfg or sa_config.load()
    if cfg.provider == "openrouter" and not cfg.api_key:
        raise LLMError("OPENROUTER_API_KEY is not set: copy .env.example to .env and add your key.")
    first = model or cfg.model
    if not first:
        raise LLMError("No model set: put SA_MODEL=<the model name your server expects> in .env.")

    failures = []
    for name in dict.fromkeys([first, *cfg.fallbacks]):  # in order, without duplicates
        payload = {"model": name, "messages": messages}
        if tools:
            payload["tools"] = tools
        try:
            data, cached = _request(cfg, payload)
        except _NextModel as e:
            failures.append(f"{name}: {e}")
            print(f"⚠️  model {name} failed ({e})")
            continue
        LEDGER.add(data.get("usage"), cached)
        return _assistant_message(data)
    raise LLMError("No model answered. " + " | ".join(failures))


# ---------------------------------------------------------------- one model: cache, retries
class _Retry(Exception):
    """A temporary failure: wait, then try the same model again."""

    def __init__(self, reason: str, wait: float | None = None):
        super().__init__(reason)
        self.wait = wait


class _NextModel(Exception):
    """This model can't answer now: move on to the next one."""


def _request(cfg: Config, payload: dict) -> tuple[dict, bool]:
    """The raw response JSON for one model (and whether it came from the cache)."""
    path = _cache_path(cfg, payload) if cfg.cache else None
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8")), True

    url = f"{cfg.base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.api_key or 'none'}"}
    if cfg.provider == "openrouter":
        headers["X-Title"] = "Student-Assistant"  # names the app on openrouter.ai (optional)

    reason = ""
    for attempt in range(cfg.retries + 1):
        try:
            data = _send(url, payload, headers, cfg.timeout)
        except _Retry as e:
            reason = str(e)
            if attempt < cfg.retries:
                wait = e.wait if e.wait is not None else 2 ** attempt + random.random()
                print(f"⏳ {payload['model']}: {reason}; retrying in {wait:.1f} s")
                _sleep(min(wait, MAX_WAIT))
            continue
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data, False
    raise _NextModel(f"gave up after {cfg.retries + 1} attempts: {reason}")


def _send(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    """One POST. Sorts every failure into retry (`_Retry`), next model (`_NextModel`) or stop (`LLMError`)."""
    try:
        res = _post(url, payload, headers, timeout)
    except requests.Timeout:
        raise _Retry(f"no answer within {timeout:g} s") from None
    except requests.ConnectionError as e:
        raise _Retry(f"cannot connect to {url}: {e}") from None

    if res.status_code in RETRY_STATUS:
        raise _Retry(f"HTTP {res.status_code}: {_detail(res)}", _retry_after(res))
    if res.status_code in NEXT_MODEL_STATUS:
        raise _NextModel(f"HTTP {res.status_code}: {_detail(res)}")
    if res.status_code >= 400:  # 400 bad request, 401 bad key, 402 no credit, 403 moderation: retrying won't help
        raise LLMError(f"HTTP {res.status_code} from {payload['model']}: {_detail(res)}")

    try:
        data = res.json()
    except ValueError:
        raise _Retry(f"the reply is not JSON: {res.text[:200]!r}") from None
    if data.get("error") or not data.get("choices"):  # some gateways answer 200 with an error inside
        raise _Retry(f"no answer in the reply: {str(data.get('error') or data)[:300]}")
    choice = data["choices"][0]
    if choice.get("finish_reason") == "error" or choice.get("error"):
        raise _Retry(f"the provider failed mid-answer: {str(choice.get('error'))[:300]}")
    return data


def _post(url: str, payload: dict, headers: dict, timeout: float) -> requests.Response:
    return requests.post(url, json=payload, headers=headers, timeout=timeout)


def _detail(res: requests.Response) -> str:
    try:
        err = res.json().get("error")
        text = err.get("message") if isinstance(err, dict) else err
    except (ValueError, AttributeError):
        text = None
    return str(text or res.text or res.reason or "")[:300]


def _retry_after(res: requests.Response) -> float | None:
    try:
        return max(0.0, float(res.headers.get("retry-after", "")))
    except ValueError:
        return None


def _cache_path(cfg: Config, payload: dict) -> Path:
    key = json.dumps({"url": cfg.base_url, **payload}, sort_keys=True, ensure_ascii=False)
    return cfg.state / "cache" / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"


# ---------------------------------------------------------------- the reply → a clean assistant message
def _assistant_message(data: dict) -> dict:
    """Keep only the standard fields. Provider extras (`reasoning_content`, ...) are dropped, `arguments` is always
    JSON text and every call has an id: some servers send dicts or omit ids, and the next request would fail."""
    msg = data["choices"][0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):  # content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))

    calls = []
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {}, ensure_ascii=False)
        calls.append({"id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                      "function": {"name": fn.get("name") or "", "arguments": args or "{}"}})

    out = {"role": "assistant", "content": content if (content or calls) else ""}
    if calls:
        out["tool_calls"] = calls
    return out
