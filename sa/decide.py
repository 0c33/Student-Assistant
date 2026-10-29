"""Sami's local decision model ("systemone"): a fast multiple-choice classifier, used as a clarify gate.

    POST $SA_DECISION_URL {"state": "...", "questions": {"next_action": {"type": "choice", "instructions": ...,
                                                                          "options": {...}}}}
    → {"answers": {"next_action": {"choice": "...", "probabilities": {...}}}, "model": "..."}

Before a task runs, the gate asks whether the request can be acted on as written. When p(ask_more) reaches
SA_DECISION_THRESHOLD (0.6, calibrated on the 19 eval prompts: docs/decision-gate.md), the agent is told to ask
Sami one clarifying question first. The gate only adds a hint: it never allows or blocks anything, so a wrong
decision costs a question, not safety. Unset SA_DECISION_URL = no gate; an unreachable service = no gate.
"""

from __future__ import annotations

import requests

from sa.config import Config

CLARIFY = {"next_action": {
    "type": "choice",
    "instructions": "Decide whether the assistant can act on the student's request as written, or must first ask "
                    "the student a clarifying question.",
    "options": {"proceed": "the request is specific enough: the notes and memory say what it refers to",
                "ask_more": "the request is ambiguous: it could mean more than one project, file or thing, and only "
                            "the student can say which"}}}
HINT = ("# HARNESS NOTE\n\nThe decision model judged this request ambiguous (p = {p:.2f}). Before acting, ask Sami one "
        "short clarifying question with user_input.")


def decide(cfg: Config, state: str, questions: dict) -> dict | None:
    """The answers, or None when the gate is off or the service fails (the task goes on without it)."""
    if not cfg.decision_url:
        return None
    try:
        r = requests.post(cfg.decision_url, json={"state": state, "questions": questions}, timeout=15)
        r.raise_for_status()
        return r.json()["answers"]
    except Exception as e:  # noqa: BLE001 - a missing gate must never stop the agent
        print(f"⚠️  decision model unavailable ({type(e).__name__}: {str(e)[:80]}); continuing without the gate")
        return None


def clarify_hint(cfg: Config, task: str, memory: str) -> str | None:
    """A system-prompt note asking the agent to clarify first, or None."""
    state = ("A student's assistant manages the student's notes. Before acting, it checks whether the request is "
             f"clear.\n\nThe student's memory:\n{memory}\n\nThe student's request: {task}")
    answers = decide(cfg, state, CLARIFY)
    if not answers:
        return None
    p = float(answers["next_action"].get("probabilities", {}).get("ask_more",
              1.0 if answers["next_action"].get("choice") == "ask_more" else 0.0))
    return HINT.format(p=p) if p >= cfg.decision_threshold else None
