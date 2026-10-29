"""The outer loop: tasks one after another, with rolling summaries and a short raw tail, saved as JSONL.

Context for each task = system prompt + summaries of earlier tasks + the raw messages of the last few tasks.
The raw tail holds whole tasks only, so it always starts on a user message and never cuts a tool result from
its call.

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
from sa.harness import Policy, Run, TerminalApproval, run_task
from sa.llm import LLMError
from sa.telemetry import AuditLog, Tracer
from sa.tools import Tools, make_tools

SUMMARY_PROMPT = """\
You summarise one finished task between Sami and the assistant, for the assistant's own memory.
Reply with ONE JSON object and nothing else (no code fences):
{"user_input": "what Sami asked or decided", "agent_response": "what the assistant did and answered: files read or changed, decisions, open items"}
Rules: never lose the meaning; never invent anything; mention each fact once; keep names, dates, numbers and file
paths exactly; at most three sentences per field; write in the language Sami used."""


class TaskSummary(BaseModel):
    user_input: str
    agent_response: str


class Session:
    """A conversation of tasks. With a `name` it is saved to, and resumed from, `.sa/sessions/<name>.jsonl`.

    Defaults are the safe ones: the sandboxed tools with the agent's own code protected, the `Policy` (writes ask),
    approval in the terminal, an audit log and traces in the state folder. `summarize=False` skips the summary
    call (one-off `ask` runs).
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
        history = [m for turn in self.turns for m in turn]
        run = run_task(task, history, self.tools, system=self.system_prompt(), chat=self.chat, model=self.cfg.model,
                       max_steps=self.cfg.max_steps, max_cost=self.cfg.max_cost, max_tokens=self.cfg.max_tokens,
                       permission=self.permission, approve=self.approve, audit=self.audit, tracer=self.tracer,
                       verbose=self.verbose)
        turn = run.history[1 + len(history):]  # this task's messages: user, ..., final assistant
        self._save({"type": "turn", "messages": turn, "stopped": run.stopped, "steps": run.steps})
        self._add_turn(turn)
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

    def system_prompt(self) -> str:
        prompt = context.build(self.cfg.root)
        if self.summaries:
            lines = "\n".join(f"{i}. {s}" for i, s in enumerate(self.summaries, 1))
            prompt += f"\n\n# EARLIER TASKS IN THIS SESSION (summaries, oldest first)\n\n{lines}"
        return prompt

    def _add_turn(self, turn: list[dict]) -> None:
        self.turns = tail(self.turns + [turn], self.cfg.keep_messages)

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
                self._add_turn(event["messages"])
            elif event.get("type") == "summary":
                self._add_summary(event["text"])
            elif event.get("type") == "new":
                self.turns = []


def tail(turns: list[list[dict]], max_messages: int) -> list[list[dict]]:
    """The most recent whole tasks that fit in `max_messages`. When even the last task is longer, keep only its
    question and final answer: never a tool result without the call that produced it."""
    kept, size = [], 0
    for turn in reversed(turns):
        if size + len(turn) > max_messages:
            break
        kept.insert(0, turn)
        size += len(turn)
    if not kept and turns:
        last = turns[-1]
        kept = [[last[0], last[-1]]]
    return kept


# ---------------------------------------------------------------- summaries
def summarize(turn: list[dict], chat: Callable[..., dict], model: str | None) -> str:
    """One finished task as text. Short tasks without tool calls are summarised without a model call (saves quota);
    if the model fails or its JSON is invalid, fall back to the plain question and answer."""
    question, answer = turn[0].get("content") or "", turn[-1].get("content") or ""
    if not any(m.get("tool_calls") for m in turn) and len(question) + len(answer) < 600:
        return _plain(question, answer)
    messages = [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": _transcript(turn)}]
    try:
        summary = parse_summary(chat(messages, None, model=model).get("content") or "")
    except (LLMError, ValueError) as e:  # pydantic's ValidationError is a ValueError
        print(f"⚠️  summary model failed, keeping the plain question and answer ({str(e)[:120]})")
        return _plain(question, answer)
    return f"Sami: {summary.user_input} → Assistant: {summary.agent_response}"


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
