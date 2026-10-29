"""Builds the system prompt: the spec, then what the vault says about Sami, then today's date, then memory.

The parts go from the most stable to the most changeable, so that consecutive requests share the longest
possible prefix (providers cache prefixes; a timestamp near the top would break that for everything after it):

    spec/AGENTS.md (never changes) → Data.md (rarely) → today's date (daily) → MEMORY.MD (per task)
    → summaries of earlier tasks (added by the session, every task)

`parts()` keeps them separate so `main.py context` can show how many tokens each one costs.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sa.config import PROJECT, Config

SPEC = PROJECT / "spec" / "AGENTS.md"


def parts(cfg: Config, now: datetime | None = None) -> list[tuple[str, str]]:
    """The system prompt as (name, text) pairs, in order."""
    out = [("spec", SPEC.read_text(encoding="utf-8").strip()),
           ("data", "# WHO YOU'RE SERVING · Data.md (from the vault)\n\n"
                    + _vault_file(cfg.root / "Data.md", "Create it when Sami shares his college data.")),
           ("date", f"# TODAY\n\nToday is {_today(cfg.tz, now)}.")]
    if cfg.memory == "inject":
        out.append(("memory", "# MEMORY.MD (current content; no need to read it again)\n\n"
                              + _vault_file(cfg.root / "MEMORY.MD", "Create it when there is something to remember.")))
    else:
        out.append(("memory", "# MEMORY.MD\n\nRead MEMORY.MD at the start of each task to resume where you left off."))
    return out


def build(cfg: Config, now: datetime | None = None) -> str:
    """The system prompt for one task. Vault files are read fresh each time, so edits show up in the next task."""
    return "\n\n".join(text for _, text in parts(cfg, now))


def estimate_tokens(obj) -> int:
    """A deliberately pessimistic token estimate (3 characters per token: Arabic text tokenizes worse than
    English). Used for budgets, where overestimating is the safe side; exact counts come from `usage`."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return len(text) // 3 + 1


def _vault_file(path: Path, hint: str) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8").strip() or f"({path.name} is empty.)"
    return f"({path.name} was not found in the vault. {hint})"


def _today(tz: str, now: datetime | None = None) -> str:
    try:
        now = now or datetime.now(ZoneInfo(tz))
        label = tz
    except ZoneInfoNotFoundError:  # no time-zone database (e.g. Windows without tzdata): use the machine's clock
        now, label = now or datetime.now().astimezone(), "local time"
    return f"{now:%A} {now.day} {now:%B %Y} ({label})"
