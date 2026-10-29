"""A scripted stand-in for the model, so the agent loop runs offline and every request it sends is recorded.

    llm = FakeLLM(use_tools(tool_call("read_file", {"file_name": "MEMORY.MD"})), answer("Done."))
    llm(messages, tools)   # → the next scripted assistant message
    llm.requests[0]        # → {"messages": [...], "tools": [...], "model": None}, deep-copied at call time

A script item may also be an exception (raised instead of replying) or a function of the messages (a reply that
depends on what the loop sent).
"""

from __future__ import annotations

import copy
import itertools
import json
from typing import Any

_ids = itertools.count(1)


def tool_call(name: str, args: dict | str | None = None, id: str | None = None) -> dict:
    """One tool call as a Chat Completions server sends it (arguments are JSON *text*; a str is sent as-is)."""
    raw = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
    return {"id": id or f"call_{next(_ids)}", "type": "function", "function": {"name": name, "arguments": raw}}


def use_tools(*calls: dict, content: str | None = None) -> dict:
    """An assistant message that calls tools."""
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


def answer(content: str) -> dict:
    """A final assistant message (no tool calls)."""
    return {"role": "assistant", "content": content}


class FakeLLM:
    """Callable like `sa.llm.chat(messages, tools=None, model=None)`; replies from a script, in order."""

    def __init__(self, *script: Any):
        self.script = list(script)
        self.requests: list[dict] = []

    def __call__(self, messages: list[dict], tools: list[dict] | None = None, model: str | None = None) -> dict:
        self.requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools), "model": model})
        if not self.script:
            raise AssertionError(f"FakeLLM script exhausted after {len(self.requests) - 1} replies")
        reply = self.script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(messages)
        return copy.deepcopy(reply)
