"""Student-Assistant: Sami's second brain over his Markdown vault.

    uv run main.py                                  chat (session "default", resumed if it exists)
    uv run main.py chat --session exams             chat in .sa/sessions/exams.jsonl
    uv run main.py ask "What's due this week?"      one task, then exit (add --session NAME to keep it)

Settings come from .env (see .env.example): the provider and model, the vault (SA_ROOT), budgets.
"""

from __future__ import annotations

import argparse
import sys

from sa import config
from sa.harness import run_task
from sa.llm import LEDGER
from sa.session import Session, chat_loop
from sa.tools import Tools


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Arabic and emoji on consoles with a legacy code page
        if (getattr(stream, "encoding", "") or "").lower() not in ("utf-8", "utf8"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session", default=argparse.SUPPRESS, metavar="NAME",
                        help="save to and resume from .sa/sessions/NAME.jsonl")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("chat", parents=[common], help="talk task by task (default)")
    ask = sub.add_parser("ask", parents=[common], help="run one task and exit")
    ask.add_argument("task")
    args = ap.parse_args(argv)
    session_name = getattr(args, "session", None)

    cfg = config.load()
    if not cfg.root.is_dir():
        if cfg.root != config.DEFAULT_ROOT:  # never create folders at a path someone typed: it may be a typo
            print(f"❌ SA_ROOT is {cfg.root}, which is not a folder. Fix SA_ROOT in .env, or remove it to use vault/.")
            return 2
        cfg.root.mkdir(parents=True)
        print(f"📁 Created the vault folder {cfg.root} (set SA_ROOT in .env to use your own).")

    if args.cmd == "ask":
        if session_name:
            run = Session(cfg, session_name).ask(args.task)
        else:  # one-off: no session file, no summary call
            run = run_task(args.task, [], Tools(cfg.root), system=Session(cfg).system_prompt(),
                           max_steps=cfg.max_steps, max_cost=cfg.max_cost)
        print(f"\n🤖 {run.final or f'(stopped: {run.stopped})'}\n\n{LEDGER}")
        return 0 if run.stopped == "done" else 1

    chat_loop(Session(cfg, session_name or "default"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
