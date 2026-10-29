"""Run the eval suite on configurations A and B (one change between them) and write per-trial CSVs.

    uv run evals/run.py --config A B --trials 3            # record: real model calls, saved to evals/results/cache/
    uv run evals/run.py --config A B --trials 3 --replay   # offline: every response from that cache, no key
    uv run evals/run.py --config A --tasks date-ai-final mem-recall --out evals/out   # a few tasks, scratch output
    uv run evals/run.py --config B --tasks ... --out evals/out --min-rate 0.6         # CI gate: exit 1 below 60%

With --replay nothing is written: the replayed rows are compared with the committed CSVs, and any difference
(other than timing) fails the run. CI does this on every push, so the published numbers stay reproducible.

The configurations differ in one thing, where MEMORY.MD comes from:
    A  SA_MEMORY=tool    the spec tells the model to read MEMORY.MD at the start of each task
    B  SA_MEMORY=inject  MEMORY.MD's content is in the system prompt

Every trial copies evals/fixtures/vault to a fresh temporary folder, pins "today" to 2026-10-29, runs the agent
non-interactively (writes auto-approved inside the sandbox, as with --yes; `user_input` answered from the task's
`user_replies`, an error once they run out) and grades the result. A crash is a failed trial, not a failed suite.
Each trial has its own cache salt, so three trials are three samples, and every one of them replays.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

EVALS = Path(__file__).resolve().parent
PROJECT = EVALS.parent
sys.path.insert(0, str(PROJECT))  # run as `uv run evals/run.py`: the sa package is one folder up

FIXTURE = EVALS / "fixtures" / "vault"
TASKS = EVALS / "tasks.jsonl"
TODAY = "2026-10-29"
CONFIGS = {"A": {"memory": "tool"}, "B": {"memory": "inject"}}
COLUMNS = ["config", "model", "task", "category", "lang", "trial", "passed", "failed_graders", "tools", "steps",
           "tokens", "seconds", "stopped"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", nargs="+", default=["A", "B"], choices=sorted(CONFIGS))
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--tasks", nargs="*", help="only these task ids")
    ap.add_argument("--replay", action="store_true", help="answer every request from <out>/cache/ (no network)")
    ap.add_argument("--out", type=Path, default=EVALS / "results", help="CSVs, transcripts, meta.json and cache/")
    ap.add_argument("--min-rate", type=float, help="exit 1 if a config's pass rate is below this (CI gate)")
    args = ap.parse_args()

    meta_path = args.out / "meta.json"
    os.environ.update({"SA_CACHE": "replay" if args.replay else "1", "SA_CACHE_DIR": str(args.out / "cache"),
                       "SA_TODAY": TODAY})
    if args.replay:  # the recorded model names; the server is never contacted
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        os.environ.update({"SA_PROVIDER": "custom", "SA_BASE_URL": "http://replay.invalid/v1", "SA_API_KEY": "none",
                           "SA_MODEL": meta["model"], "SA_SMALL_MODEL": meta["small_model"],
                           "SA_FALLBACKS": ",".join(meta["fallbacks"])})

    from sa import config, llm  # after the environment is set: config.load() reads it

    base = config.load()
    tasks = [json.loads(line) for line in TASKS.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.replay and not args.tasks:  # replay exactly what was recorded
        recorded = set()
        for name in args.config:
            if (args.out / f"{name}.csv").is_file():
                with (args.out / f"{name}.csv").open(encoding="utf-8") as f:
                    recorded |= {r["task"] for r in csv.DictReader(f)}
        args.tasks = sorted(recorded)
    if args.tasks:
        tasks = [t for t in tasks if t["id"] in args.tasks]
    print(f"{len(tasks)} tasks × {args.trials} trials × configs {args.config} · model {base.model} · "
          f"{'replay' if args.replay else 'record'} · today {TODAY}\n")

    summaries, mismatches = {}, []
    for name in args.config:
        rows = [run_trial(task, name, k, base, args.out, save=not args.replay)
                for task in tasks for k in range(args.trials)]
        if args.replay:
            mismatches += compare(args.out / f"{name}.csv", rows)
        else:
            write_csv(args.out / f"{name}.csv", rows)
        summaries[name] = summarize(rows)
    if not args.replay:
        args.out.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({"model": base.model, "small_model": base.small_model,
                                         "fallbacks": list(base.fallbacks), "provider": base.provider,
                                         "today": TODAY, "trials": args.trials, "configs": CONFIGS,
                                         "recorded": time.strftime("%Y-%m-%d")}, indent=1), encoding="utf-8")

    print(f"\n{'config':<8}{'trials':>7}{'pass rate':>11}{'pass@k':>8}{'pass^k':>8}{'avg steps':>11}{'avg tokens':>12}")
    for name, s in summaries.items():
        print(f"{name:<8}{s['trials']:>7}{s['pass_rate']:>11.2f}{s['pass_at_k']:>8.2f}{s['pass_hat_k']:>8.2f}"
              f"{s['avg_steps']:>11.1f}{s['avg_tokens']:>12,.0f}")
    print(f"\n{llm.LEDGER}\nCSVs and transcripts: {args.out.relative_to(PROJECT) if args.out.is_relative_to(PROJECT) else args.out}")
    status = 0
    if args.replay:
        print("\n✅ The replay matches the committed results." if not mismatches else
              "\n❌ The replay differs from the committed results:\n  " + "\n  ".join(mismatches[:20]))
        status = 1 if mismatches else 0
    if args.min_rate is not None:
        low = [n for n, s in summaries.items() if s["pass_rate"] < args.min_rate]
        print(f"\nGate {args.min_rate:.2f}: " + (f"❌ below it: {low}" if low else "✅ passed"))
        status = status or (1 if low else 0)
    return status


def run_trial(task: dict, config_name: str, k: int, base, out: Path, save: bool = True) -> dict:
    from sa.harness import Run
    from sa.session import Session
    from sa.tools import make_tools

    from evals.graders import grade

    work = Path(tempfile.mkdtemp(prefix="sa-eval-"))
    vault = work / "vault"
    shutil.copytree(FIXTURE, vault)
    cfg = replace(base, root=vault.resolve(), state=work / "state", memory=CONFIGS[config_name]["memory"])
    os.environ["SA_CACHE_SALT"] = f"{config_name}/{task['id']}/{k}"
    session = Session(cfg, None, tools=make_tools(cfg, ask=scripted(task.get("user_replies", []))),
                      approve=lambda name, args: True, summarize=False, verbose=False)
    started = time.perf_counter()
    try:
        run, error = session.ask(task["prompt"]), ""
    except Exception as e:  # noqa: BLE001 - a crash is a failed trial
        run, error = Run("", [], 0, "crash"), f"{type(e).__name__}: {str(e)[:200]}"
    seconds = time.perf_counter() - started
    grades = grade(task, run.final, run.tool_calls, vault, FIXTURE)
    passed = not error and run.stopped == "done" and all(g["pass"] for g in grades)
    failed = "; ".join(f"{g['type']}: {g['detail']}" for g in grades if not g["pass"])
    if run.stopped != "done":
        failed = f"stopped: {run.stopped}" + (f" ({error})" if error else "") + (f"; {failed}" if failed else "")

    changed = {p.relative_to(vault).as_posix(): p.read_text(encoding="utf-8", errors="replace")
               for p in vault.rglob("*") if p.is_file()
               and (not (FIXTURE / p.relative_to(vault)).is_file()
                    or p.read_bytes() != (FIXTURE / p.relative_to(vault)).read_bytes())}
    transcript = out / "transcripts" / config_name / f"{task['id']}-{k}.json"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    save and transcript.write_text(json.dumps({"task": task, "config": config_name, "trial": k, "passed": passed,
                                      "stopped": run.stopped, "final": run.final, "grades": grades,
                                      "files_changed_or_new": changed,
                                      "messages": run.history[1:]},  # without the system prompt (spec/AGENTS.md)
                                     ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)

    print(f"{'✅' if passed else '❌'} {config_name} {task['id']:<22} #{k}  {failed[:100]}")
    return {"config": config_name, "model": base.model, "task": task["id"], "category": task["category"],
            "lang": task["lang"], "trial": k, "passed": passed, "failed_graders": failed,
            "tools": " ".join(n for n, _ in run.tool_calls), "steps": run.steps, "tokens": run.tokens,
            "seconds": round(seconds, 1), "stopped": run.stopped}


def scripted(replies: list[str]):
    """`user_input` answered from the task's script; once the script runs out, the tool returns an error."""
    queue = list(replies)

    def ask(question: str) -> str:
        if not queue:
            raise RuntimeError("no scripted answer left in this eval: decide without asking")
        return queue.pop(0)

    return ask


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def compare(path: Path, rows: list[dict]) -> list[str]:
    """Differences between replayed rows and a committed CSV, ignoring timing."""
    if not path.is_file():
        return [f"{path.name}: no committed results to compare with"]
    with path.open(encoding="utf-8") as f:
        committed = {(r["task"], r["trial"]): r for r in csv.DictReader(f)}
    diffs = []
    for row in rows:
        old = committed.get((row["task"], str(row["trial"])))
        if old is None:
            diffs.append(f"{row['config']} {row['task']} #{row['trial']}: not in {path.name}")
            continue
        for col in COLUMNS:
            if col != "seconds" and str(row[col]) != old[col]:
                diffs.append(f"{row['config']} {row['task']} #{row['trial']} {col}: {old[col]!r} → {row[col]!r}")
    return diffs


def summarize(rows: list[dict]) -> dict:
    """Pass rate over trials; pass@k (a task passed in some trial) and pass^k (in every trial) over tasks."""
    by_task: dict[str, list[bool]] = {}
    for r in rows:
        by_task.setdefault(r["task"], []).append(r["passed"])
    n = len(rows) or 1
    return {"trials": len(rows), "pass_rate": sum(r["passed"] for r in rows) / n,
            "pass_at_k": sum(any(v) for v in by_task.values()) / (len(by_task) or 1),
            "pass_hat_k": sum(all(v) for v in by_task.values()) / (len(by_task) or 1),
            "avg_steps": sum(r["steps"] for r in rows) / n, "avg_tokens": sum(r["tokens"] for r in rows) / n}


if __name__ == "__main__":
    sys.exit(main())
