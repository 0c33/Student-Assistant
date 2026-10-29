"""llm.chat over a fake HTTP layer: retries, fallbacks, errors, the ledger, the cache, message clean-up."""

import copy
import json

import pytest
import requests

from sa import config, llm
from sa.llm import LLMError


class FakeResponse:
    def __init__(self, status: int = 200, body=None, headers: dict | None = None):
        self.status_code, self._body = status, body
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.text = json.dumps(body) if body is not None else ""
        self.reason = ""

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def completion(content="ok", tool_calls=None, **message_extra) -> FakeResponse:
    message = {"role": "assistant", "content": content, **message_extra}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return FakeResponse(200, {"id": "gen-1", "model": "m", "choices": [{"index": 0, "message": message,
                              "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


class FakeServer:
    """Stands in for `llm._post`: replies from a queue (a FakeResponse, an exception, or a function of the payload)."""

    def __init__(self):
        self.replies, self.requests, self.sleeps = [], [], []

    def __call__(self, url, payload, headers, timeout):
        self.requests.append({"url": url, "payload": copy.deepcopy(payload), "headers": headers, "timeout": timeout})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply(payload) if callable(reply) else reply

    @property
    def models(self):
        return [r["payload"]["model"] for r in self.requests]


@pytest.fixture
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setattr(llm, "_post", s)
    monkeypatch.setattr(llm, "_sleep", s.sleeps.append)
    monkeypatch.setattr(llm, "LEDGER", llm.Ledger())
    monkeypatch.setenv("SA_MODEL", "main/model:free")
    monkeypatch.setenv("SA_FALLBACKS", "backup/model:free")
    return s


MESSAGES = [{"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]


def test_sends_model_tools_key_and_timeout(server):
    server.replies = [completion("hello")]
    msg = llm.chat(MESSAGES, TOOLS)

    sent = server.requests[0]
    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent["payload"] == {"model": "main/model:free", "messages": MESSAGES, "tools": TOOLS}
    assert sent["headers"]["Authorization"] == "Bearer test-key-not-real"
    assert sent["timeout"] == 120
    assert msg == {"role": "assistant", "content": "hello"}
    assert (llm.LEDGER.calls, llm.LEDGER.prompt_tokens, llm.LEDGER.completion_tokens) == (1, 10, 5)


def test_no_tools_means_no_tools_field(server):
    server.replies = [completion()]
    llm.chat(MESSAGES)
    assert "tools" not in server.requests[0]["payload"]


def test_rate_limit_is_retried_after_the_servers_wait(server):
    server.replies = [FakeResponse(429, {"error": {"message": "rate limited"}}, {"Retry-After": "3"}), completion()]
    assert llm.chat(MESSAGES)["content"] == "ok"
    assert server.models == ["main/model:free", "main/model:free"]
    assert server.sleeps == [3.0]


def test_network_errors_and_timeouts_are_retried(server):
    server.replies = [requests.ConnectionError("refused"), requests.Timeout(), completion()]
    assert llm.chat(MESSAGES)["content"] == "ok"
    assert len(server.requests) == 3 and len(server.sleeps) == 2


def test_falls_back_to_the_next_model_after_retries(server):
    server.replies = [FakeResponse(503, {"error": {"message": "overloaded"}})] * 3 + [completion("from backup")]
    assert llm.chat(MESSAGES)["content"] == "from backup"
    assert server.models == ["main/model:free"] * 3 + ["backup/model:free"]  # SA_RETRIES=2 → 3 attempts


def test_missing_model_goes_straight_to_the_fallback(server):
    server.replies = [FakeResponse(404, {"error": {"message": "No endpoints found"}}), completion()]
    llm.chat(MESSAGES)
    assert server.models == ["main/model:free", "backup/model:free"]


def test_a_200_without_choices_is_retried(server):
    server.replies = [FakeResponse(200, {"error": {"message": "upstream error", "code": 502}}), completion()]
    assert llm.chat(MESSAGES)["content"] == "ok"


def test_bad_request_raises_at_once(server):
    server.replies = [FakeResponse(400, {"error": {"message": "tool_call_id not found"}})]
    with pytest.raises(LLMError, match="HTTP 400 .*tool_call_id not found"):
        llm.chat(MESSAGES)
    assert len(server.requests) == 1


def test_every_model_failing_raises_llm_error(server):
    server.replies = [FakeResponse(503)] * 6
    with pytest.raises(LLMError, match="No model answered.*main/model:free.*backup/model:free"):
        llm.chat(MESSAGES)


def test_missing_key_fails_before_any_request(server, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        llm.chat(MESSAGES)
    assert server.requests == []


def test_custom_provider_uses_its_url_and_needs_a_model(server, monkeypatch):
    monkeypatch.setenv("SA_PROVIDER", "custom")
    monkeypatch.setenv("SA_BASE_URL", "http://localhost:8000/v1/")
    monkeypatch.setenv("SA_FALLBACKS", "")
    monkeypatch.setenv("SA_MODEL", "")
    with pytest.raises(LLMError, match="SA_MODEL"):
        llm.chat(MESSAGES)

    monkeypatch.setenv("SA_MODEL", "qwen3.6-35b-a3b")
    server.replies = [completion()]
    llm.chat(MESSAGES)
    assert server.requests[0]["url"] == "http://localhost:8000/v1/chat/completions"
    assert server.models == ["qwen3.6-35b-a3b"]


def test_reply_is_cleaned_into_a_standard_assistant_message(server):
    calls = [{"function": {"name": "read_file", "arguments": {"file_name": "MEMORY.MD"}}}]  # dict args, no id
    server.replies = [completion(None, calls, reasoning_content="thinking...")]
    msg = llm.chat(MESSAGES, TOOLS)

    assert set(msg) == {"role", "content", "tool_calls"}  # reasoning_content dropped
    call = msg["tool_calls"][0]
    assert call["id"].startswith("call_")
    assert call["type"] == "function"
    assert json.loads(call["function"]["arguments"]) == {"file_name": "MEMORY.MD"}


def test_empty_content_without_calls_becomes_an_empty_string(server):
    server.replies = [completion(None)]
    assert llm.chat(MESSAGES) == {"role": "assistant", "content": ""}


def test_cache_replays_identical_requests(server, monkeypatch):
    monkeypatch.setenv("SA_CACHE", "1")
    server.replies = [completion("first")]
    assert llm.chat(MESSAGES)["content"] == "first"
    assert llm.chat(MESSAGES)["content"] == "first"  # no second reply queued: it must come from disk
    assert len(server.requests) == 1
    assert llm.LEDGER.cache_hits == 1
    assert list((config.load().state / "cache").glob("*.json"))


def test_a_long_retry_after_moves_to_the_next_model_without_waiting(server):
    server.replies = [FakeResponse(429, {"error": {"message": "free-models-per-day"}}, {"Retry-After": "3600"}),
                      completion("from backup")]
    assert llm.chat(MESSAGES)["content"] == "from backup"
    assert server.models == ["main/model:free", "backup/model:free"]
    assert server.sleeps == []


def test_retry_sleeping_is_capped_per_call_across_models(server):
    """At most MAX_WAIT seconds of sleeping per chat() call, so a stalled demo gives up within the 20 s rule."""
    slow = FakeResponse(429, {"error": {"message": "slow down"}}, {"Retry-After": "15"})
    server.replies = [slow, slow, completion("from backup")]
    assert llm.chat(MESSAGES)["content"] == "from backup"
    assert server.sleeps == [15.0]  # the second wait (15 s) would exceed the 5 s left: next model, no sleep
    assert server.models == ["main/model:free", "main/model:free", "backup/model:free"]

    server.replies, server.sleeps = [slow, slow, slow], []
    with pytest.raises(LLMError):  # the backup asks for 15 s too: over budget, so the call fails fast
        llm.chat(MESSAGES)
    assert sum(server.sleeps) <= llm.MAX_WAIT


def test_ledger_records_which_model_answered(server):
    server.replies = [FakeResponse(404), completion()]
    llm.chat(MESSAGES)
    assert llm.LEDGER.last_model == "m"  # the "model" field of the reply; the request went to the fallback
    assert llm.LEDGER.tokens == 15
