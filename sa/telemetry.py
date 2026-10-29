"""What the harness leaves behind for people: an audit log of tool calls and traces of every task.

- `AuditLog` → `.sa/audit.jsonl`: one line per tool call (time, tool, redacted args, decision, outcome, size).
- `Tracer` → `.sa/traces.jsonl`: spans named after the OpenTelemetry GenAI semantic conventions
  (`gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.tool.name`, ...), so any
  OTel backend could ingest them; written as plain JSON lines, with no OpenTelemetry dependency.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

# Saudi national ID / iqama (10 digits starting 1 or 2) and Saudi mobile numbers: never written to a log
ID_RE = re.compile(r"(?<!\d)[12]\d{9}(?!\d)")
MOBILE_RE = re.compile(r"(?<![\d+])(?:\+966|00966|0)?5\d{8}(?!\d)")


def redact(text: str, width: int | None = 200) -> str:
    """Logs and summaries keep the shape of a text, not its ID or phone numbers (nor, with a width, its bulk)."""
    text = MOBILE_RE.sub("[REDACTED]", ID_RE.sub("[REDACTED]", text))
    return text if width is None or len(text) <= width else f"{text[:width]}… [{len(text) - width} chars cut]"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class AuditLog:
    """Callable: `audit(tool=..., args=..., decision=..., ok=..., result_chars=...)` appends one line."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def __call__(self, *, tool: str, args: dict, decision: str, ok: bool, result_chars: int) -> None:
        _append(self.path, {"ts": _now(), "tool": tool,
                            "args": {k: redact(v) if isinstance(v, str) else v for k, v in args.items()},
                            "decision": decision, "ok": ok, "result_chars": result_chars})


class Tracer:
    """Spans as JSON lines. A span opened with no parent starts a new trace (one per task)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stack: list[dict] = []

    @contextmanager
    def span(self, name: str, **attributes) -> Iterator[dict]:
        """Open a span; the yielded dict holds its attributes, so the caller can add some before it closes."""
        parent = self._stack[-1] if self._stack else None
        span = {"trace_id": parent["trace_id"] if parent else uuid.uuid4().hex, "span_id": uuid.uuid4().hex[:16],
                "parent_id": parent["span_id"] if parent else None, "name": name, "start": _now(),
                "attributes": dict(attributes)}
        self._stack.append(span)
        started = time.perf_counter()
        try:
            yield span["attributes"]
        except BaseException as e:
            span["attributes"]["error.type"] = type(e).__name__
            raise
        finally:
            self._stack.pop()
            span["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            _append(self.path, span)
