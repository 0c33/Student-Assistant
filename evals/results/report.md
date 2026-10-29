Model `qwen3.8-flash-next-iq3s` (judge: `qwen3.8-flash-next-iq3s`), recorded 2026-10-29, today pinned to 2026-10-29, 1 trials per task per config. Reproduce offline: `uv run evals/run.py --config A B --trials 1 --replay`.

| Config | Memory | Trials | Pass rate | pass@k | pass^k (k = 1) | Avg requests | Avg tokens |
|---|---|---:|---:|---:|---:|---:|---:|
| A | read with `read_file` (spec instruction) | 15 | 1.00 | 1.00 | 1.00 | 3.5 | 13,108 |
| B | injected in the system prompt | 15 | 1.00 | 1.00 | 1.00 | 3.1 | 12,299 |

| Category | A passed | B passed |
|---|---:|---:|
| clarify | 1/1 | 1/1 |
| dates | 3/3 | 3/3 |
| editing | 1/1 | 1/1 |
| honesty | 1/1 | 1/1 |
| memory | 5/5 | 5/5 |
| planning | 1/1 | 1/1 |
| retrieval | 1/1 | 1/1 |
| security | 2/2 | 2/2 |

**What flipped between A and B** (trials passed out of 1; tasks with the same result left out):

| Task | A | B | Direction |
|---|---:|---:|---|
| (none) | | | |

**Error analysis** (every failed trial, labelled from its transcript in `evals/results/transcripts/`):

| Root cause | Count (A) | Count (B) | Example task | Fix tried |
|---|---:|---:|---|---|
| (no failures) | 0 | 0 | | |

**Judge check:** 5 judged answers labelled independently before reading the verdict (who labelled them: `evals/results/judge_check.csv`); the judge agreed on 5/5. All of them were passes, so this shows the judge doesn't fail good answers, not that it catches bad ones.
