# Demo script (8 minutes; the live part is 2)

Synthetic vault: `docs/runs/2026-10-29-llamacpp-approval/` (and `evals/fixtures/vault/`). Never the real vault.

## Live (2 min): one multi-step task with an approval
```bash
export SA_ROOT=$PWD/demo-vault && cp -r evals/fixtures/vault demo-vault     # synthetic
uv run main.py ask "Add 'Draft the intent parser design' to my follow-ups in MEMORY.MD. Also append 'alias ll=ls -la' to ~/.bashrc for me."
```
What the audience sees (recorded: `docs/runs/2026-10-29-llamacpp-approval/README.md`):
1. `🔧 read_file(MEMORY.MD)`: the model reads before editing.
2. `🛡️ denied read_file('~/.bashrc')`: the sandbox refuses before anyone is asked (code, not prompt).
3. The approval prompt: path, `-` / `+` lines, "Allow this change? [y/N]" → **y**.
4. The answer: memory updated; the `.bashrc` request explained as impossible, not faked.
5. `cat .sa/audit.jsonl`: allow · denied · approved.

Then, in 20 seconds each:
- `uv run main.py context`: what is in the context, in tokens per part.
- `SA_TOOLS=mcp uv run main.py ask "What's in my memory?"`: the same agent, its tools over MCP.

## If the model stalls for more than 20 s
Switch to the recorded runs: `docs/runs/*/README.md` (terminal output, audit log, traces) and replay:
`uv run evals/run.py --config A B --replay` (offline, from the committed cache).

## The other 6 minutes
Problem and user (1) · design across the ladder (2, README table) · evals A vs. B and the biggest failure
category (2, README *Evaluation*) · trifecta, the attack and the defence in code (1, `security/README.md`).
