# Run · 2026-10-29 · the agent on MCP tools (stdio), real model

Evidence for Phase 4 (capstone requirement 1: a tool consumed over MCP). The agent ran with `SA_TOOLS=mcp`: its
vault tools came from `servers/vault_mcp.py`, launched as a subprocess and spoken to over stdio. `SA_MEMORY=tool`
made the model fetch MEMORY.MD through the server instead of finding it in the prompt. Synthetic vault.

| | |
|---|---|
| Engine / model | llama.cpp server on the author's LAN (`SA_PROVIDER=custom`), `qwen3.8-flash-next-iq3s` |
| Command | `SA_TOOLS=mcp SA_MEMORY=tool uv run main.py ask "What's in my memory? And is there a note about my voice assistant project?"` |
| Result | `stopped: done`, 3 model calls, 3 MCP tool calls (`read_file` → `search_vault` → `read_file`), 8,831 prompt + 314 completion tokens |

`terminal.txt` is the full output; `traces.jsonl` shows the three `execute_tool` spans inside one `invoke_agent`
trace; `audit.jsonl` the three calls (all reads: `allow`). The policy, approvals and audit log are the same as with
local tools, because `McpTools` gives the harness the same interface (`tests/test_mcp.py`).
