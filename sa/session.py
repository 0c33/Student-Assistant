"""The outer loop: tasks one after another, with rolling summaries and a short raw tail, saved as JSONL.

Context for each task = system prompt (spec, Data.md, date, MEMORY.MD, summaries of earlier tasks) + the raw
messages of the most recent whole tasks that fit the token budget (SA_CONTEXT_TOKENS, for the whole request).
That is the compaction rule: older tasks survive only as their summaries. The raw tail holds whole tasks only, so
it always starts on a user message and never cuts a tool result from its call. A task that stopped on a model
error keeps only its question and the harness note: one oversized result can't poison the rest of the session.

Session file `.sa/sessions/<name>.jsonl`, one event per line (append-only, easy to read and to replay):
    {"type": "turn", "messages": [...], "stopped": "done", "steps": 2, "ts": "..."}
    {"type": "summary", "text": "...", "ts": "..."}
    {"type": "new", "ts": "..."}       /new: later tasks start without the raw tail (the summaries stay)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

from pydantic import BaseModel

from sa import context, llm
from sa.config import Config
from sa.context import estimate_tokens
from sa.harness import Policy, Run, TerminalApproval, cap_results, run_task
from sa.llm import LLMError
from sa.telemetry import AuditLog, Tracer, redact
from sa.tools import Tools, make_tools

SUMMARY_PROMPT = """\
You summarise one finished task between Sami and the assistant, for the assistant's own memory.
Reply with ONE JSON object and nothing else (no code fences):
{"user_input": "what Sami asked or decided", "agent_response": "what the assistant did and answered: files read or changed, decisions, open items"}
Rules: never lose the meaning; never invent anything; mention each fact once; keep decisions, dates and file paths
exactly; never copy ID numbers, phone numbers, passwords or other identifiers (write "an ID number" instead);
at most three sentences per field; write in the language Sami used."""


class TaskSummary(BaseModel):
    user_input: str
    agent_response: str


class Session:
    """A conversation of tasks. With a `name` it is saved to, and resumed from, `.sa/sessions/<name>.jsonl`.

    Defaults are the safe ones: the sandboxed tools with the agent's own code protected, the `Policy` (writes ask),
    approval in the terminal, tool results capped, an audit log and traces in the state folder.
    `summarize=False` skips the summary call (one-off `ask` runs).
    """

    def __init__(self, cfg: Config, name: str | None = None, *, tools: Tools | None = None,
                 chat: Callable[..., dict] | None = None,
                 permission: Callable[[str, dict], str] | None = None,
                 approve: Callable[[str, dict], bool] | None = None,
                 summarize: bool = True, verbose: bool = True):
        self.cfg, self.name, self.chat, self.summarize, self.verbose = cfg, name, chat, summarize, verbose
        self.tools = tools or make_tools(cfg)
        self.permission = permission or Policy(self.tools)
        self.approve = approve or TerminalApproval(self.tools)
        self.audit = AuditLog(cfg.state / "audit.jsonl")
        self.tracer = Tracer(cfg.state / "traces.jsonl")
        self.turns: list[list[dict]] = []  # the raw tail: whole tasks, oldest first
        self.summaries: list[str] = []  # rolling, oldest first
        self.path = cfg.state / "sessions" / f"{name}.jsonl" if name else None
        if self.path and self.path.exists():
            self._load()

    def ask(self, task: str) -> Run:
        """Run one task with the session's context, then save it and summarise it."""
        system = self.system_prompt()
        history = self.history(system)
        run = run_task(task, history, self.tools, system=system, chat=self.chat, model=self.cfg.model,
                       max_steps=self.cfg.max_steps, max_cost=self.cfg.max_cost, max_tokens=self.cfg.max_tokens,
                       permission=self.permission, approve=self.approve,
                       after_tool=cap_results(self.cfg.max_result_chars),
                       audit=self.audit, tracer=self.tracer, verbose=self.verbose)
        turn = run.history[1 + len(history):]  # this task's messages: user, ..., final assistant
        self._save({"type": "turn", "messages": turn, "stopped": run.stopped, "steps": run.steps})
        self._add_turn(turn, run.stopped)
        if not self.summarize:
            return run

        summary = summarize(turn, self.chat or llm.chat, self.cfg.small_model)
        self._save({"type": "summary", "text": summary})
        self._add_summary(summary)
        if self.verbose:
            print(f"📝 {summary[:160]}{'…' if len(summary) > 160 else ''}")
        return run

    def new(self) -> None:
        """Start the next task fresh: drop the raw tail, keep the summaries."""
        self.turns = []
        self._save({"type": "new"})

    def prompt_parts(self) -> list[tuple[str, str]]:
        """The system prompt as (name, text) parts: context.parts() plus the summaries of earlier tasks."""
        out = context.parts(self.cfg)
        if self.summaries:
            lines = "\n".join(f"{i}. {s}" for i, s in enumerate(self.summaries, 1))
            out.append(("summaries", f"# EARLIER TASKS IN THIS SESSION (summaries, oldest first)\n\n{lines}"))
        return out

    def system_prompt(self) -> str:
        return "\n\n".join(text for _, text in self.prompt_parts())

    def history(self, system: str | None = None) -> list[dict]:
        """The raw messages to send: the most recent whole tasks that fit what the system prompt and the tool
        definitions leave of the request budget."""
        system = self.system_prompt() if system is None else system
        left = self.cfg.context_tokens - estimate_tokens(system) - estimate_tokens(self.tools.schemas())
        return [m for turn in tail(self.turns, left) for m in turn]

    def _add_turn(self, turn: list[dict], stopped: str = "done") -> None:
        if stopped == "error":  # whatever broke the request (e.g. an oversized result) must not be resent
            turn = [turn[0], turn[-1]]
        self.turns = tail(self.turns + [turn], self.cfg.context_tokens)

    def _add_summary(self, summary: str) -> None:
        self.summaries = (self.summaries + [summary])[-self.cfg.keep_summaries:]

    def _save(self, event: dict) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {**event, "ts": datetime.now().astimezone().isoformat(timespec="seconds")}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"⚠️  {self.path.name} line {n} is not JSON: skipped")
                continue
            if event.get("type") == "turn":
                self._add_turn(event["messages"], event.get("stopped", "done"))
            elif event.get("type") == "summary":
                self._add_summary(event["text"])
            elif event.get("type") == "new":
                self.turns = []


def tail(turns: list[list[dict]], max_tokens: int) -> list[list[dict]]:
    """The most recent whole tasks that fit in `max_tokens` (estimated). When even the last task is larger, keep
    only its question and final answer: never a tool result without the call that produced it."""
    kept, size = [], 0
    for turn in reversed(turns):
        cost = estimate_tokens(turn)
        if size + cost > max_tokens:
            break
        kept.insert(0, turn)
        size += cost
    if not kept and turns:
        last = turns[-1]
        kept = [[last[0], last[-1]]]
    return kept


# ---------------------------------------------------------------- summaries
def summarize(turn: list[dict], chat: Callable[..., dict], model: str | None) -> str:
    """One finished task as text, with ID and phone numbers redacted. Short tasks without tool calls are
    summarised without a model call (saves quota); if the model fails or its JSON is invalid, fall back to the
    plain question and answer."""
    question, answer = turn[0].get("content") or "", turn[-1].get("content") or ""
    if not any(m.get("tool_calls") for m in turn) and len(question) + len(answer) < 600:
        return redact(_plain(question, answer), width=None)
    messages = [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": _transcript(turn)}]
    try:
        summary = parse_summary(chat(messages, None, model=model).get("content") or "")
    except (LLMError, ValueError) as e:  # pydantic's ValidationError is a ValueError
        print(f"⚠️  summary model failed, keeping the plain question and answer ({str(e)[:120]})")
        return redact(_plain(question, answer), width=None)
    return redact(f"Sami: {summary.user_input} → Assistant: {summary.agent_response}", width=None)


def parse_summary(text: str) -> TaskSummary:
    """The JSON object in a model reply (code fences and chatter around it are ignored), validated."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in the summary reply: {text[:100]!r}")
    return TaskSummary.model_validate_json(text[start:end + 1])


def _transcript(turn: list[dict], width: int = 800) -> str:
    lines = []
    for m in turn:
        if m["role"] == "user":
            lines.append(f"Sami: {_short(m.get('content'), width)}")
        elif m["role"] == "assistant":
            if m.get("content"):
                lines.append(f"Assistant: {_short(m['content'], width)}")
            for call in m.get("tool_calls") or []:
                lines.append(f"Assistant called {call['function']['name']}({_short(call['function']['arguments'], 300)})")
        elif m["role"] == "tool":
            lines.append(f"Tool result: {_short(m.get('content'), 400)}")
    return "\n".join(lines)


def _plain(question: str, answer: str) -> str:
    return f"Sami: {_short(question, 200)} → Assistant: {_short(answer, 300)}"


def _short(text: str | None, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[:width] + "…"


# ---------------------------------------------------------------- context anatomy
def anatomy(session: Session) -> list[tuple[str, int]]:
    """Estimated tokens of each part of the next request: the system prompt's parts, the tool definitions, and
    the history by role. Answers "what's in the context, and how big is it?" with numbers."""
    rows = [(f"system · {name}", estimate_tokens(text)) for name, text in session.prompt_parts()]
    rows.append(("tool definitions", estimate_tokens(session.tools.schemas())))
    history = session.history()
    for role in ("user", "assistant", "tool"):
        rows.append((f"history · {role} messages", sum(estimate_tokens(m) for m in history if m["role"] == role)))
    return rows


# ---------------------------------------------------------------- the interactive loop
def chat_loop(session: Session, read: Callable[[str], str] = input) -> None:
    """Read a task, run it, wait for the next. /new = fresh context, /quit or an empty line = exit."""
    where = f"session '{session.name}'" if session.name else "unsaved session"
    print(f"Student-Assistant · {where} · vault: {session.cfg.root}")
    print("Type a task. /new = start fresh (summaries stay) · /quit or an empty line = exit.")
    if session.turns or session.summaries:
        print(f"↩️  Resumed: {len(session.summaries)} task summaries, {sum(map(len, session.turns))} recent messages.")
    while True:
        try:
            line = read("\n🧑 ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line or line == "/quit":
            break
        if line == "/new":
            session.new()
            print("🆕 Fresh start: the next task sees the summaries, not the raw messages.")
            continue
        if line.startswith("/"):
            print("Commands: /new, /quit (or an empty line).")
            continue
        try:
            run = session.ask(line)
        except KeyboardInterrupt:
            print("\n⏹️  Task interrupted: not saved.")
            continue
        print(f"\n🤖 {run.final or f'(stopped: {run.stopped})'}")
    print(f"👋 Bye. {llm.LEDGER}")
