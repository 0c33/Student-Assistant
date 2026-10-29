"""Graders for the eval suite: each checks the final answer, the tools used, or the vault the run left behind.

A task passes when all its graders pass. Grader types (the course's, plus file checks for a vault agent):
- contains_any     {"values": [...]}               the final answer contains at least one value
- not_contains     {"values": [...]}               the final answer contains none of them
- tool_called      {"name": ...}                   the agent called this tool at least once
- tool_not_called  {"name": ...}                   it never called this tool
- file_exists      {"path": ...}                   the file exists in the vault afterwards
- file_contains    {"path": ..., "values": [...]}  the file contains every value
- file_not_contains{"path": ..., "values": [...]}  the file contains none of them
- file_unchanged   {"path": ...}                   the file is byte-for-byte what the fixture had
- file_changed     {"path": ...}                   the file differs from the fixture
- new_file         {"under": ..., "values": [...]} some file that wasn't in the fixture (under that prefix)
                                                    contains every value
- judge            {"rubric": ...}                 an LLM judge (SA_SMALL_MODEL) decides whether the answer meets it

Text comparisons are case-insensitive and read Arabic-Indic digits (٢٢) as ASCII (22).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:  # run as `uv run evals/run.py`: the sa package is one folder up
    sys.path.insert(0, str(PROJECT))

from sa import llm  # noqa: E402
from sa.config import load  # noqa: E402

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

JUDGE_PROMPT = """You grade one reply of a student's assistant (it manages the student's Markdown notes).
Decide whether the REPLY meets the RUBRIC. Judge only what the rubric asks; ignore style unless it says otherwise.
Reply with JSON only: {"pass": true or false, "reason": "one sentence"}"""


def norm(text: str) -> str:
    return (text or "").translate(ARABIC_DIGITS).casefold()


def judge(rubric: str, prompt: str, reply: str, tools_used: list[str], chat: Callable[..., dict] | None = None) -> dict:
    """An LLM judge on the small model. Like every call it goes through the cache, so re-grading is free."""
    user = f"RUBRIC:\n{rubric}\n\nUSER MESSAGE:\n{prompt}\n\nTOOLS THE ASSISTANT CALLED: {tools_used or 'none'}\n\nREPLY:\n{reply}"
    try:
        msg = (chat or llm.chat)([{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": user}],
                                 None, model=load().small_model)
    except llm.LLMError as e:
        return {"pass": False, "reason": f"judge unavailable: {e}"}
    text = msg.get("content") or ""
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        return {"pass": bool(data.get("pass")), "reason": str(data.get("reason", ""))}
    except (ValueError, AttributeError):
        return {"pass": False, "reason": f"unparseable judge output: {text[:120]}"}


def grade(task: dict, final: str, tool_calls: list[tuple[str, dict]], vault: Path, fixture: Path,
          chat: Callable[..., dict] | None = None) -> list[dict]:
    """Apply every grader of a task. Returns one {"type", "pass", "detail"} per grader."""
    names = [n for n, _ in tool_calls]
    out = []
    for g in task["graders"]:
        kind = g["type"]
        path = vault / g["path"] if "path" in g else None
        text = path.read_text(encoding="utf-8") if path is not None and path.is_file() else None
        if kind == "contains_any":
            ok = any(norm(v) in norm(final) for v in g["values"])
            detail = "" if ok else f"answer has none of {g['values'][:4]}"
        elif kind == "not_contains":
            hits = [v for v in g["values"] if norm(v) in norm(final)]
            ok, detail = not hits, f"answer contains {hits}" if hits else ""
        elif kind == "tool_called":
            ok, detail = g["name"] in names, "" if g["name"] in names else f"{g['name']} not called"
        elif kind == "tool_not_called":
            ok, detail = g["name"] not in names, "" if g["name"] not in names else f"{g['name']} was called"
        elif kind == "file_exists":
            ok, detail = text is not None, "" if text is not None else f"{g['path']} missing"
        elif kind == "file_contains":
            missing = [v for v in g["values"] if text is None or norm(v) not in norm(text)]
            ok, detail = not missing, f"{g['path']} lacks {missing}" if missing else ""
        elif kind == "file_not_contains":
            hits = [v for v in g["values"] if text is not None and norm(v) in norm(text)]
            ok, detail = not hits, f"{g['path']} still has {hits}" if hits else ""
        elif kind in ("file_unchanged", "file_changed"):
            same = text == (fixture / g["path"]).read_text(encoding="utf-8")
            ok = same if kind == "file_unchanged" else not same
            detail = "" if ok else f"{g['path']} {'changed' if kind == 'file_unchanged' else 'unchanged'}"
        elif kind == "new_file":
            found = [p for p in new_files(vault, fixture, g.get("under", ""))
                     if all(norm(v) in norm(p.read_text(encoding="utf-8", errors="replace")) for v in g["values"])]
            ok = bool(found)
            detail = "" if ok else f"no new file under {g.get('under') or 'the vault'!r} with {g['values']}"
        elif kind == "judge":
            verdict = judge(g["rubric"], task["prompt"], final, names, chat)
            ok, detail = verdict["pass"], verdict["reason"]
        else:
            raise ValueError(f"unknown grader {kind!r}")
        out.append({"type": kind, "pass": ok, "detail": detail})
    return out


def new_files(vault: Path, fixture: Path, under: str = "") -> list[Path]:
    """Files in the vault after the run that the fixture didn't have."""
    before = {p.relative_to(fixture) for p in fixture.rglob("*") if p.is_file()}
    return [p for p in sorted(vault.rglob("*")) if p.is_file() and p.relative_to(vault) not in before
            and p.relative_to(vault).as_posix().startswith(under)]
