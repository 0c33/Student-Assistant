"""Hand-label judged answers before seeing the judge's verdict, then compare (Lab 3.4's "Is the judge right?").

    uv run evals/label_judge.py            # 5 judged answers from config A; writes evals/results/judge_check.csv
    uv run evals/label_judge.py --n 8 --config B

For each answer it shows the task, the rubric, the tools used and the final answer, never the verdict, and asks
pass or fail. Then it prints the agreement and saves the labels; `evals/report.py` puts the result in the README.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="A")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--labeller", default="Sami, by hand")
    args = ap.parse_args()
    judged = []
    for f in sorted((RESULTS / "transcripts" / args.config).glob("*.json")):
        t = json.loads(f.read_text(encoding="utf-8"))
        verdicts = [g for g in t["grades"] if g["type"] == "judge"]
        if verdicts:
            judged.append((t, verdicts[0]))
    rows = []
    for t, verdict in judged[:args.n]:
        rubric = next(g["rubric"] for g in t["task"]["graders"] if g["type"] == "judge")
        tools = [c["function"]["name"] for m in t["messages"] for c in (m.get("tool_calls") or [])]
        print(f"\n{'=' * 90}\nTASK {t['task']['id']}: {t['task']['prompt']}\nRUBRIC: {rubric}\nTOOLS: {tools}\n"
              f"{'-' * 90}\n{t['final']}\n{'-' * 90}")
        label = ""
        while label not in ("pass", "fail"):
            label = {"p": "pass", "f": "fail"}.get(input("Your verdict, before seeing the judge's [p/f]: ").strip().lower()[:1], "")
        judge = "pass" if verdict["pass"] else "fail"
        print(f"Judge: {judge} ({verdict['detail']})  →  {'agree' if label == judge else 'DISAGREE'}")
        rows.append({"config": args.config, "task": t["task"]["id"], "trial": t["trial"], "human": label,
                     "judge": judge, "note": f"labeller: {args.labeller}, before reading the verdict"})
    with (RESULTS / "judge_check.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["config", "task", "trial", "human", "judge", "note"])
        writer.writeheader()
        writer.writerows(rows)
    agree = sum(r["human"] == r["judge"] for r in rows)
    print(f"\nAgreement: {agree}/{len(rows)}. Saved to evals/results/judge_check.csv; run `uv run evals/report.py`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
