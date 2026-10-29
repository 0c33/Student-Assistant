"""The inner loop: one task → model ↔ tools, until a final answer or a budget stops it.

Every tool call goes: parse arguments → permission → (approve) → before_tool → execute → after_tool → audit.
- permission(name, args) → "allow" | "ask" | "deny" (or "deny: <reason>"). Default: `Policy`: reads run, writes
  ask a human, and calls the sandbox would refuse are denied without asking.
- approve(name, args) → bool, for "ask". Default: `TerminalApproval`, which shows what the write would change.
- before_tool(name, args) may return a string: it replaces the call (e.g. a policy refusal).
- after_tool(name, args, result) may return a string: it replaces the result (e.g. a trimmed one).

Approval is not the last line of defence: the sandbox and the protected paths are enforced inside the tools
(`sa/tools.py`), so they hold even for a call a human approved.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

from sa import llm
from sa.llm import LLMError
from sa.telemetry import AuditLog, Tracer
from sa.tools import WRITE_TOOLS, Tools, error

STANDARD_FIELDS = ("role", "content", "tool_calls")  # what we store and send back; provider extras are dropped
ICONS = {"call": "🔧", "result": "📦", "error": "⚠️ ", "guard": "🛡️ ", "model": "🤖", "stop": "⏹️ "}


@dataclass
class Run:
    final: str  # the last answer ("" when a budget or an error stopped the task)
    history: list[dict]  # system, earlier messages, user, assistant(tool_calls), tool, ..., assistant
    steps: int  # model calls made
    stopped: str  # done | max_steps | max_cost | max_tokens | error
    cost: float = 0.0
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    tokens: int = 0


# ---------------------------------------------------------------- permission and approval
class Policy:
    """The default permission policy. Reads run; writes ask a human; anything the sandbox would refuse (outside the
    vault, the assistant's own code, secrets) is denied before anyone is asked."""

    def __init__(self, tools: Tools):
        self.tools = tools

    def __call__(self, name: str, args: dict) -> str:
        reason = self.tools.refusal(name, args)
        if reason:
            return f"deny: {reason}"
        return "ask" if name in WRITE_TOOLS else "allow"


def allow_all(name: str, args: dict) -> str:
    """No policy (the security test's `--unsafe` run uses it to show what the policy prevents)."""
    return "allow"


class TerminalApproval:
    """Ask Sami in the terminal, showing exactly what the write would change."""

    def __init__(self, tools: Tools):
        self.tools = tools

    def __call__(self, name: str, args: dict) -> bool:
        print("\n" + preview(self.tools, name, args))
        if self.tools.tainted:
            print(f"⚠️  This task read untrusted content ({', '.join(sorted(self.tools.tainted))}). "
                  f"Approve only if this is what YOU asked for.")
        try:
            reply = input("🛡️  Allow this change? [y/N] ")
        except (EOFError, OSError):  # no terminal: nobody approved
            return False
        return reply.strip().lower() in ("y", "yes", "نعم")


class AutoApproval:
    """`--yes`: approve writes without asking, except once the task has read untrusted content (inbox/, downloads/):
    then a write may be what a note asked for rather than what Sami asked for, so it needs a human and is refused
    here. The sandbox and protected paths apply either way."""

    def __init__(self, tools: Tools):
        self.tools = tools

    def __call__(self, name: str, args: dict) -> bool:
        if self.tools.tainted:
            print(f"🛡️  not auto-approved: this task read untrusted content ({', '.join(sorted(self.tools.tainted))}); "
                  f"{name} needs a human")
            return False
        print(f"🛡️  auto-approved (--yes): {name}")
        return True


def preview(tools: Tools, name: str, args: dict, max_lines: int = 12) -> str:
    """What a write would do, as a small diff: the path, then - lines removed and + lines added."""
    path = str(args.get("file_name") or args.get("directory_path") or "")
    head, body = f"🛡️  {name} → {path}", []
    if name == "write_file":
        exists = (tools.root / path).is_file()
        mode = args.get("write_mode", "a")
        head += " (new file)" if not exists else (" (OVERWRITES the whole file)" if mode == "w" else " (append)")
        body = _diff_lines("+", str(args.get("content", "")), max_lines)
    elif name in ("replace_in_file", "remove_from_file"):
        old = str(args.get("old_content") or args.get("content_to_remove") or "")
        body = _diff_lines("-", old, max_lines) + _diff_lines("+", str(args.get("new_content", "")), max_lines)
        if args.get("replace_all"):
            head += " (every occurrence)"
    elif name != "make_dir":
        body = [f"   {json.dumps(args, ensure_ascii=False)[:300]}"]
    return "\n".join([head, *body])


def cap_results(max_chars: int) -> Callable[[str, dict, str], str | None]:
    """An after_tool hook: a result longer than `max_chars` is cut, with a note saying how much and how to get the
    rest. One long note can't flood the context window."""
    def cap(name: str, args: dict, result: str) -> str | None:
        if len(result) <= max_chars:
            return None
        return (result[:max_chars] + f"\n[… {len(result) - max_chars:,} characters cut. Read a smaller part: "
                f"read_file with start_line/max_lines, or search_vault.]")
    return cap


def _diff_lines(sign: str, text: str, max_lines: int) -> list[str]:
    lines = text.splitlines()
    out = [f"   {sign} {line}" for line in lines[:max_lines]]
    if len(lines) > max_lines:
        out.append(f"   … {len(lines) - max_lines} more lines")
    return out


# ---------------------------------------------------------------- the loop
def run_task(task: str, history: list[dict], tools: Tools, *, system: str,
             chat: Callable[..., dict] | None = None, model: str | None = None,
             max_steps: int = 20, max_cost: float | None = None, max_tokens: int | None = None,
             permission: Callable[[str, dict], str] | None = None,
             approve: Callable[[str, dict], bool] | None = None,
             before_tool: Callable[[str, dict], str | None] | None = None,
             after_tool: Callable[[str, dict, str], str | None] | None = None,
             audit: AuditLog | None = None, tracer: Tracer | None = None,
             ledger: llm.Ledger | None = None, verbose: bool = True) -> Run:
    """Drive one task to completion. `history` is the earlier conversation (no system message).

    `chat` and `ledger` default to `llm.chat` and `llm.LEDGER`, looked up now (not when this module was imported).
    `permission` and `approve` default to `Policy(tools)` and `TerminalApproval(tools)`: writes always ask.
    """
    chat, ledger = chat or llm.chat, ledger or llm.LEDGER
    permission, approve = permission or Policy(tools), approve or TerminalApproval(tools)
    span = tracer.span if tracer else _no_span
    show = _printer(verbose)
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": task}]
    schemas = tools.schemas()
    cost0, prompt0, completion0, calls_made = ledger.cost, ledger.prompt_tokens, ledger.completion_tokens, []

    def stop(steps: int, stopped: str, reason: str) -> Run:
        """End early. A closing assistant note keeps the history well formed (no dangling tool results) and tells
        the model, next time, that this task did not finish."""
        show("stop", reason)
        messages.append({"role": "assistant", "content": f"[harness] Task stopped: {reason}. It is not finished."})
        return Run("", messages, steps, stopped, ledger.cost - cost0, calls_made, ledger.tokens - prompt0 - completion0)

    with span("invoke_agent student-assistant", **{"gen_ai.operation.name": "invoke_agent",
                                                   "gen_ai.agent.name": "student-assistant"}) as task_span:
        run = None
        for step in range(1, max_steps + 1):
            with span(f"chat {model or 'default'}", **{"gen_ai.operation.name": "chat",
                                                       "gen_ai.request.model": model or "default"}) as chat_span:
                prompt, completion = ledger.prompt_tokens, ledger.completion_tokens
                try:
                    msg = _clean(chat(messages, schemas, model=model))
                except LLMError as e:
                    chat_span["error.type"] = "LLMError"
                    run = stop(step, "error", f"model error: {e}")
                    break
                finally:
                    chat_span.update({"gen_ai.response.model": ledger.last_model,
                                      "gen_ai.usage.input_tokens": ledger.prompt_tokens - prompt,
                                      "gen_ai.usage.output_tokens": ledger.completion_tokens - completion})

            calls = msg.get("tool_calls") or []  # servers send a list, [] or null
            messages.append(msg)  # the FULL assistant message, tool_calls included, before any tool result
            if not calls:
                run = Run(msg.get("content") or "", messages, step, "done", ledger.cost - cost0, calls_made,
                          ledger.tokens - prompt0 - completion0)
                break
            if msg.get("content"):
                show("model", msg["content"])

            for call in calls:
                with span(f"execute_tool {call['function']['name']}",
                          **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": call["function"]["name"],
                             "gen_ai.tool.call.id": call["id"]}) as tool_span:
                    name, args, out, decision = _run_call(call, tools, permission, approve, before_tool,
                                                          after_tool, show)
                    ok = not out.startswith('{"error"')
                    tool_span.update({"sa.decision": decision, **({} if ok else {"error.type": "tool_error"})})
                calls_made.append((name, args))
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})
                if audit:
                    audit(tool=name, args=args, decision=decision, ok=ok, result_chars=len(out))

            if max_cost is not None and ledger.cost - cost0 > max_cost:
                run = stop(step, "max_cost", f"cost budget ${max_cost} used up")
                break
            if max_tokens is not None and ledger.tokens - prompt0 - completion0 > max_tokens:
                run = stop(step, "max_tokens", f"token budget of {max_tokens:,} used up")
                break
        else:
            run = stop(max_steps, "max_steps", f"step budget of {max_steps} model calls used up")

        task_span.update({"sa.stopped": run.stopped, "sa.steps": run.steps, "sa.tool_calls": len(calls_made),
                          "gen_ai.usage.input_tokens": ledger.prompt_tokens - prompt0,
                          "gen_ai.usage.output_tokens": ledger.completion_tokens - completion0})
    return run


def _run_call(call: dict, tools: Tools, permission, approve, before_tool, after_tool,
              show) -> tuple[str, dict, str, str]:
    """One tool call through the pipeline. Returns (name, parsed args, what the model will see, decision), where
    decision is allow | approved | refused | denied | blocked | bad_args."""
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
        return name, {}, out, "bad_args"

    show("call", f"{name}({json.dumps(args, ensure_ascii=False)})")
    verdict, _, reason = permission(name, args).partition(":")
    decision = verdict.strip()
    if decision == "ask":
        decision = "approved" if approve(name, args) else "refused"
    elif decision != "allow":  # "deny", or anything unexpected: fail closed
        decision = "denied"

    if decision == "refused":
        out = error(f"Sami did not approve {name}. Don't retry it; ask Sami what to do instead, or carry on without it.")
    elif decision == "denied":
        out = error(f"The harness denied {name}: {reason.strip() or 'not permitted here'} "
                    f"Tell Sami what you wanted to do instead.")
    else:
        out = before_tool(name, args) if before_tool else None
        if out is not None:
            decision = "blocked"
        else:
            out = tools.call(name, args)
        if after_tool:
            out = after_tool(name, args, out) or out
    if decision in ("refused", "denied", "blocked"):
        show("guard", f"{decision} {name}: {out}")
    else:
        show("error" if out.startswith('{"error"') else "result", out)
    return name, args, out, decision


def _clean(msg: dict) -> dict:
    """Keep only the standard fields: some providers reject their own extras (e.g. reasoning_content) when sent back."""
    out = {k: msg[k] for k in STANDARD_FIELDS if k in msg}
    out["role"] = "assistant"
    if not out.get("tool_calls"):
        out.pop("tool_calls", None)
        out["content"] = out.get("content") or ""  # an assistant message without tool calls needs content
    return out


@contextmanager
def _no_span(name: str, **attributes):
    yield {}


def _printer(verbose: bool) -> Callable[[str, str], None]:
    def show(kind: str, text: str, width: int = 200) -> None:
        if verbose:
            text = " ".join(str(text).split())
            print(f"{ICONS.get(kind, '·')} {text[:width]}{'…' if len(text) > width else ''}")
    return show
