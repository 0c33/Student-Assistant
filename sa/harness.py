"""The inner loop: one task → model ↔ tools, until a final answer or a budget stops it.

Every tool call goes: parse arguments → permission → before_tool → execute → after_tool.
- permission(name, args) → "allow" | "ask" | "deny"; "ask" calls approve(name, args) → bool (default: the terminal).
- before_tool(name, args) may return a string: it replaces the call (e.g. a policy refusal).
- after_tool(name, args, result) may return a string: it replaces the result (e.g. a trimmed one).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from sa import llm
from sa.llm import LLMError
from sa.tools import Tools, error

STANDARD_FIELDS = ("role", "content", "tool_calls")  # what we store and send back; provider extras are dropped
ICONS = {"call": "🔧", "result": "📦", "error": "⚠️ ", "guard": "🛡️ ", "model": "🤖", "stop": "⏹️ "}


@dataclass
class Run:
    final: str  # the last answer ("" when a budget or an error stopped the task)
    history: list[dict]  # system, earlier messages, user, assistant(tool_calls), tool, ..., assistant
    steps: int  # model calls made
    stopped: str  # done | max_steps | max_cost | error
    cost: float = 0.0
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)


def run_task(task: str, history: list[dict], tools: Tools, *, system: str,
             chat: Callable[..., dict] = llm.chat, model: str | None = None,
             max_steps: int = 20, max_cost: float | None = None,
             permission: Callable[[str, dict], str] | None = None,
             approve: Callable[[str, dict], bool] | None = None,
             before_tool: Callable[[str, dict], str | None] | None = None,
             after_tool: Callable[[str, dict, str], str | None] | None = None,
             ledger: llm.Ledger = llm.LEDGER, verbose: bool = True) -> Run:
    """Drive one task to completion. `history` is the earlier conversation (no system message)."""
    show = _printer(verbose)
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": task}]
    schemas = tools.schemas()
    start_cost, calls_made = ledger.cost, []

    for step in range(1, max_steps + 1):
        try:
            msg = _clean(chat(messages, schemas, model=model))
        except LLMError as e:
            show("error", f"model error: {e}")
            return _stop(messages, step, "error", f"model error: {e}", ledger.cost - start_cost, calls_made)

        calls = msg.get("tool_calls") or []  # servers send a list, [] or null
        messages.append(msg)  # the FULL assistant message, tool_calls included, before any tool result
        if not calls:
            return Run(msg.get("content") or "", messages, step, "done", ledger.cost - start_cost, calls_made)
        if msg.get("content"):
            show("model", msg["content"])

        for call in calls:
            name, args, out = _run_call(call, tools, permission, approve, before_tool, after_tool, show)
            calls_made.append((name, args))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})

        if max_cost is not None and ledger.cost - start_cost > max_cost:
            return _stop(messages, step, "max_cost", f"cost budget ${max_cost} used up",
                         ledger.cost - start_cost, calls_made, show)

    return _stop(messages, max_steps, "max_steps", f"step budget of {max_steps} model calls used up",
                 ledger.cost - start_cost, calls_made, show)


def _run_call(call: dict, tools: Tools, permission, approve, before_tool, after_tool, show) -> tuple[str, dict, str]:
    """One tool call through the pipeline. Returns (name, parsed args, what the model will see)."""
    name = call["function"]["name"]
    raw = call["function"].get("arguments") or "{}"
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(args, dict):
            raise ValueError("not a JSON object")
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        show("call", f"{name}({raw})")
        out = error(f"The arguments for {name} are not a valid JSON object ({e}). Send a JSON object.")
        show("error", out)
        return name, {}, out

    show("call", f"{name}({json.dumps(args, ensure_ascii=False)})")
    decision = permission(name, args) if permission else "allow"
    if decision == "ask":
        decision = "allow" if (approve or approve_in_terminal)(name, args) else "deny"
    if decision != "allow":  # "deny", or anything unexpected: fail closed
        out = error(f"The harness denied {name}: not permitted. Tell Sami what you wanted to do instead.")
        show("guard", f"denied {name}")
        return name, args, out

    out = before_tool(name, args) if before_tool else None
    if out is not None:
        show("guard", f"before_tool replaced {name}")
    else:
        out = tools.call(name, args)
    if after_tool:
        out = after_tool(name, args, out) or out
    show("error" if out.startswith('{"error"') else "result", out)
    return name, args, out


def approve_in_terminal(name: str, args: dict) -> bool:
    try:
        reply = input(f"\n🛡️  Allow {name}({json.dumps(args, ensure_ascii=False)})? [y/N] ")
    except EOFError:
        return False
    return reply.strip().lower() in ("y", "yes")


def _clean(msg: dict) -> dict:
    """Keep only the standard fields: some providers reject their own extras (e.g. reasoning_content) when sent back."""
    out = {k: msg[k] for k in STANDARD_FIELDS if k in msg}
    out["role"] = "assistant"
    if not out.get("tool_calls"):
        out.pop("tool_calls", None)
        out["content"] = out.get("content") or ""  # an assistant message without tool calls needs content
    return out


def _stop(messages: list[dict], steps: int, stopped: str, reason: str, cost: float,
          calls_made: list, show: Callable | None = None) -> Run:
    """End a task early. A closing assistant note keeps the history well formed (no dangling tool results) and tells
    the model, next time, that this task did not finish."""
    if show:
        show("stop", reason)
    messages.append({"role": "assistant", "content": f"[harness] Task stopped: {reason}. It is not finished."})
    return Run("", messages, steps, stopped, cost, calls_made)


def _printer(verbose: bool) -> Callable[[str, str], None]:
    def show(kind: str, text: str, width: int = 200) -> None:
        if verbose:
            text = " ".join(str(text).split())
            print(f"{ICONS.get(kind, '·')} {text[:width]}{'…' if len(text) > width else ''}")
    return show
