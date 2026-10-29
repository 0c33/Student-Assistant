"""The agent as an MCP host: the vault tools come from `servers/vault_mcp.py` over MCP instead of in-process.

    SA_TOOLS=mcp uv run main.py ask "What's in my memory?"

`MCPBridge` runs the official async client (`mcp.Client`) on a background event loop, so the synchronous harness
can call it (the pattern of the course's `agentkit.MCPBridge`). `McpTools` gives the harness the same interface as
`sa.tools.Tools` (schemas, call, refusal, root), so the policy, approvals, audit log and traces work unchanged.
`user_input` stays local: asking the human is the host's job, not the server's.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from sa.config import PROJECT, Config
from sa.tools import Tools, ask_in_terminal, error, make_tools, schema

SERVER = PROJECT / "servers" / "vault_mcp.py"


class MCPBridge:
    """A synchronous handle on an MCP client session. `server` is anything `mcp.Client` accepts: an `MCPServer`
    object (in-process, for tests), `StdioServerParameters` (a subprocess) or a Streamable HTTP URL."""

    def __init__(self, server: Any):
        self.server = server

    def __enter__(self) -> "MCPBridge":
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        ready: Future = Future()
        # one long-lived task opens and closes the client (anyio scopes must exit in the task that entered them)
        self._main = asyncio.run_coroutine_threadsafe(self._session(ready), self.loop)
        wait([ready, self._main], timeout=60, return_when=FIRST_COMPLETED)
        if not ready.done():
            self._stop_loop()
            self._main.result(0) if self._main.done() else None  # re-raise the connection error, if any
            raise TimeoutError("the MCP server did not start within 60 s")
        self.client = ready.result()
        return self

    async def _session(self, ready: Future) -> None:
        from mcp import Client

        self._closed = asyncio.Event()
        async with Client(self.server) as client:
            ready.set_result(client)
            await self._closed.wait()

    def run(self, coro, timeout: float = 120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def list_tools(self) -> list:
        return self.run(self.client.list_tools()).tools

    def call(self, name: str, arguments: dict) -> Any:
        return self.run(self.client.call_tool(name, arguments))

    def read_resource(self, uri: str) -> str:
        return "\n".join(getattr(c, "text", "") for c in self.run(self.client.read_resource(uri)).contents)

    def _stop_loop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)

    def __exit__(self, *exc) -> None:
        try:
            self.loop.call_soon_threadsafe(self._closed.set)
            self._main.result(30)
        finally:
            self._stop_loop()


class McpTools:
    """The MCP server's tools, served to the harness through the same interface as `sa.tools.Tools`.

    `local` is a `Tools` on the same vault, used only to (1) pre-check the sandbox so the policy can deny an escape
    before anyone is asked (the server refuses it anyway) and (2) run `user_input`, which stays in the host.
    """

    def __init__(self, bridge: MCPBridge, local: Tools):
        self.bridge, self.local, self.root = bridge, local, local.root
        self._schemas = {t.name: _chat_schema(t) for t in bridge.list_tools()}
        self._schemas["user_input"] = schema(local.user_input)
        self.registry = {name: None for name in self._schemas}  # names, for listings

    def schemas(self) -> list[dict]:
        return list(self._schemas.values())

    def refusal(self, name: str, args: dict) -> str | None:
        return self.local.refusal(name, args)

    def call(self, name: str, args: dict) -> str:
        if name == "user_input":
            return self.local.call(name, args)
        if name not in self._schemas:
            return error(f"Unknown tool {name!r}. Available tools: {', '.join(self._schemas)}.")
        try:
            result = self.bridge.call(name, args)
        except Exception as e:  # noqa: BLE001 - transport failures reach the model as errors, not crashes
            return error(f"{name} failed over MCP: {type(e).__name__}: {e}")
        text = "\n".join(getattr(c, "text", "") or "" for c in result.content)
        return error(text) if result.is_error else text


def _chat_schema(tool: Any) -> dict:
    """An MCP tool → a Chat Completions tool. Pydantic's "title" and "default" keys are dropped (not every
    provider accepts them; the defaults are stated in the description, which carries the Args section)."""
    params = {k: v for k, v in (tool.input_schema or {"type": "object", "properties": {}}).items() if k != "title"}
    params["properties"] = {name: {k: v for k, v in prop.items() if k not in ("title", "default")}
                            for name, prop in params.get("properties", {}).items()}
    return {"type": "function", "function": {"name": tool.name, "description": tool.description or "",
                                             "parameters": params}}


def server_params(cfg: Config):
    """How to launch the vault server as a subprocess: this Python (its environment has `mcp`), the vault, the
    write tools on (the harness asks the human before every write), and only the SA_* settings in its env."""
    from mcp import StdioServerParameters

    env = {k: v for k, v in os.environ.items() if k.startswith("SA_") or k in ("PATH", "HOME", "LANG", "SYSTEMROOT")}
    return StdioServerParameters(command=sys.executable, args=[str(SERVER), "--root", str(cfg.root), "--write"],
                                 env=env)


@contextmanager
def open_tools(cfg: Config, ask: Callable[[str], str] = ask_in_terminal,
               allow_self_edit: bool = False) -> Iterator[Tools | McpTools]:
    """The tools for one run of the CLI: in-process (SA_TOOLS=local) or from the MCP server (SA_TOOLS=mcp)."""
    local = make_tools(cfg, ask=ask, allow_self_edit=allow_self_edit)
    if cfg.tools == "local":
        yield local
        return
    print(f"🔌 Vault tools over MCP: {SERVER.relative_to(PROJECT)} (stdio)")
    with MCPBridge(server_params(cfg)) as bridge:
        yield McpTools(bridge, local)
