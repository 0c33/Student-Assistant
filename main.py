"""Student-Assistant: Sami's second brain over his Markdown vault.

    uv run main.py                                  chat (session "default", resumed if it exists)
    uv run main.py chat --session exams             chat in .sa/sessions/exams.jsonl
    uv run main.py ask "What's due this week?"      one task, then exit (add --session NAME to keep it)
    uv run main.py context --session exams          what the next request would contain, in tokens per part

Every write to the vault asks for approval first (--yes approves them all, for evals). The sandbox always applies:
nothing outside the vault, and never the assistant's own code (--allow-self-edit lifts only the latter).
Settings come from .env (see .env.example): the provider and model, the vault (SA_ROOT), budgets.
"""

from __future__ import annotations

import argparse
import sys

from sa import config
from sa.harness import auto_approve
from sa.llm import LEDGER
from sa.mcp_client import open_tools
from sa.session import Session, anatomy, chat_loop


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Arabic and emoji on consoles with a legacy code page
        if (getattr(stream, "encoding", "") or "").lower() not in ("utf-8", "utf8"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session", default=argparse.SUPPRESS, metavar="NAME",
                        help="save to and resume from .sa/sessions/NAME.jsonl")
    common.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                        help="approve every write without asking (evals); the sandbox still applies")
    common.add_argument("--allow-self-edit", action="store_true", default=argparse.SUPPRESS,
                        help="let the agent change its own code (main.py, sa/, spec/); off by default")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("chat", parents=[common], help="talk task by task (default)")
    ask = sub.add_parser("ask", parents=[common], help="run one task and exit")
    ask.add_argument("task")
    sub.add_parser("context", parents=[common], help="show the token cost of each part of the next request")
    args = ap.parse_args(argv)
    session_name = getattr(args, "session", None)
    yes, self_edit = getattr(args, "yes", False), getattr(args, "allow_self_edit", False)

    cfg = config.load()
    if not cfg.root.is_dir():
        if cfg.root != config.DEFAULT_ROOT:  # never create folders at a path someone typed: it may be a typo
            print(f"❌ SA_ROOT is {cfg.root}, which is not a folder. Fix SA_ROOT in .env, or remove it to use vault/.")
            return 2
        cfg.root.mkdir(parents=True)
        print(f"📁 Created the vault folder {cfg.root} (set SA_ROOT in .env to use your own).")

    if self_edit:
        print("⚠️  --allow-self-edit: the agent may change its own code (writes still ask for approval).")
    approve = auto_approve if yes else None  # None: ask in the terminal
    with open_tools(cfg, allow_self_edit=self_edit) as tools:  # in-process, or over MCP with SA_TOOLS=mcp
        return dispatch(args.cmd or "chat", args, cfg, tools, session_name, approve)


def dispatch(cmd: str, args: argparse.Namespace, cfg: config.Config, tools, session_name: str | None, approve) -> int:
    if cmd == "context":
        session = Session(cfg, session_name or "default", tools=tools)
        rows = anatomy(session)
        total = sum(n for _, n in rows)
        print(f"Next request in session '{session.name}' (estimated at 3 characters per token):\n")
        for part, n in rows:
            print(f"  {part:<28} {n:>7,}  {n / total:6.1%}" if total else f"  {part:<28} {n:>7,}")
        print(f"  {'total':<28} {total:>7,}  (budget SA_CONTEXT_TOKENS={cfg.context_tokens:,})")
        return 0

    if cmd == "ask":  # one task; without --session nothing is saved and no summary is made
        session = Session(cfg, session_name, tools=tools, approve=approve, summarize=bool(session_name))
        run = session.ask(args.task)
        print(f"\n🤖 {run.final or f'(stopped: {run.stopped})'}\n\n{LEDGER}")
        return 0 if run.stopped == "done" else 1

    chat_loop(Session(cfg, session_name or "default", tools=tools, approve=approve))
    return 0


if __name__ == "__main__":
    sys.exit(main())
