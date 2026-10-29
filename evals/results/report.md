Model `qwen3.8-flash-next-iq3s` (judge: `qwen3.8-flash-next-iq3s`), recorded 2026-10-29, today pinned to 2026-10-29, 1 trials per task per config. Reproduce offline: `uv run evals/run.py --config A B --trials 1 --replay`.

| Config | Memory | Trials | Pass rate | pass@k | pass^k (k = 1) | Avg requests | Avg tokens |
|---|---|---:|---:|---:|---:|---:|---:|
| A | read with `read_file` (spec instruction) | 10 | 1.00 | 1.00 | 1.00 | 2.6 | 0 |

| Category | A passed |
|---|---:|
| dates | 3/3 |
| editing | 1/1 |
| memory | 5/5 |
| retrieval | 1/1 |

**Error analysis** (every failed trial, labelled from its transcript in `evals/results/transcripts/`):

| Root cause | Count (A) | Count (B) | Example task | Fix tried |
|---|---:|---:|---|---|
| (no failures) | 0 | 0 | | |
