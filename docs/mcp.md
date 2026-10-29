# The vault over MCP

`servers/vault_mcp.py` serves the vault to any MCP host. It is a single-file PEP 723 script (`uv run` installs
`mcp` for it) built with the official SDK's `MCPServer`, as in the course's `labs/servers/madar_mcp.py`.

| | |
|---|---|
| Tools (default) | `read_file`, `search_vault`, `list_dir`: annotated `readOnlyHint` |
| Tools (`--write`) | + `write_file`, `replace_in_file`, `remove_from_file`, `make_dir`: annotated `destructiveHint` |
| Resources | `vault://memory` (MEMORY.MD), `vault://data` (Data.md) |
| Transports | stdio (default); Streamable HTTP with `--http PORT`, bound to 127.0.0.1 only |
| Sandbox | the server runs the agent's own `sa.tools.Tools`: no path outside the vault, no `.env`, no writes to the assistant's code or state, MEMORY.MD's size cap. Enforced by the server, whatever the host does. |
| Approval | the host's job: writes carry `destructiveHint`. Student-Assistant's harness asks before every write. |

## Student-Assistant as the host
`SA_TOOLS=mcp` makes the agent launch the server over stdio and use its tools (`sa/mcp_client.py`:
`MCPBridge` + `McpTools`). `user_input` stays in the host. Evidence: `docs/runs/2026-10-29-llamacpp-mcp/`;
tests: `tests/test_mcp.py` (in-process server, the agent on MCP tools with the same policy, and a real stdio
subprocess through `main.main`).

## Another host: Streamable HTTP with the official client (2026-10-29)
```text
$ uv run servers/vault_mcp.py --root <synthetic vault> --http 8765
$ python -c '… async with mcp.Client("http://127.0.0.1:8765/mcp") as c: …'
server: student-vault | protocol: 2026-07-28
tools: ['read_file', 'search_vault', 'list_dir']
search_vault → {"matches": [{"path": "projects/voice-assistant.md", "line": 2, "text": "Status: speech-to-text works, intent parser missing."}], "total": 1}
escape → is_error: True | Error executing tool read_file: read_file failed: PermissionError: '../../../etc/hostname' leads outside the v…
```

## Registering it in Claude Code
```bash
claude mcp add student-vault -- uv run /path/to/Student-Assistant/servers/vault_mcp.py --root /path/to/vault
```
or, per project, in `.mcp.json`:
```json
{"mcpServers": {"student-vault": {"command": "uv", "args": ["run", "servers/vault_mcp.py", "--root", "vault"]}}}
```
Without `--write` the host gets the three read-only tools, which is the safe default for a host you don't control.
