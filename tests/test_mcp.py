"""MCP: the vault server on its own, the agent consuming it (in-process and over stdio), with the same policy."""

import json

import pytest

import main
from sa import config, llm
from sa.harness import run_task
from sa.mcp_client import MCPBridge, McpTools, open_tools
from sa.tools import Tools
from servers import vault_mcp
from tests.fake_llm import FakeLLM, answer, tool_call, use_tools


@pytest.fixture
def vault(isolated_env):
    (isolated_env / "MEMORY.MD").write_text("## Pending\n- revise chapter 3\n", encoding="utf-8")
    (isolated_env / "Data.md").write_text("## Exams\n- AI final: 20 November 2026\n", encoding="utf-8")
    return isolated_env


@pytest.fixture
def bridge(vault):
    with MCPBridge(vault_mcp.build(vault, write=True)) as b:  # in-process: same protocol, no subprocess
        yield b


def test_read_only_by_default_and_annotated(vault):
    with MCPBridge(vault_mcp.build(vault)) as b:
        tools = {t.name: t for t in b.list_tools()}
    assert list(tools) == ["read_file", "search_vault", "list_dir"]
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools.values())
    assert "Args:" in tools["read_file"].description  # the model sees the argument docs


def test_write_flag_adds_destructive_tools(bridge):
    tools = {t.name: t for t in bridge.list_tools()}
    assert set(tools) == {"read_file", "search_vault", "list_dir", "write_file", "replace_in_file",
                          "remove_from_file", "make_dir"}
    assert tools["write_file"].annotations.destructive_hint and not tools["write_file"].annotations.read_only_hint


def test_the_server_enforces_the_same_sandbox(bridge, vault):
    ok = bridge.call("read_file", {"file_name": "MEMORY.MD"})
    assert not ok.is_error and "revise chapter 3" in ok.content[0].text
    for name, args in [("read_file", {"file_name": "../../etc/passwd"}),
                       ("write_file", {"file_name": "/tmp/x.md", "content": "x"}),
                       ("read_file", {"file_name": ".env"})]:
        r = bridge.call(name, args)
        assert r.is_error and "PermissionError" in r.content[0].text


def test_resources(bridge):
    assert "revise chapter 3" in bridge.read_resource("vault://memory")
    assert "AI final" in bridge.read_resource("vault://data")


def test_mcp_tools_look_like_local_tools(bridge, vault):
    remote, local = McpTools(bridge, Tools(vault)), Tools(vault)
    names = lambda ts: sorted(s["function"]["name"] for s in ts.schemas())  # noqa: E731
    assert names(remote) == names(local)  # 7 over MCP + user_input kept in the host
    for s in remote.schemas():
        for prop in s["function"]["parameters"]["properties"].values():
            assert "title" not in prop and "default" not in prop


def test_the_agent_runs_on_mcp_tools_with_the_same_policy(bridge, vault):
    tools = McpTools(bridge, Tools(vault, ask=lambda q: "the graduation project"))
    fake = FakeLLM(use_tools(tool_call("read_file", {"file_name": "MEMORY.MD"}, id="r"),
                             tool_call("user_input", {"question": "Which project?"}, id="u"),
                             tool_call("write_file", {"file_name": "plan.md", "content": "x"}, id="w"),
                             tool_call("write_file", {"file_name": "../out.md", "content": "x"}, id="e")),
                   answer("ok"))
    asked = []
    run = run_task("Plan it", [], tools, system="S", chat=fake, verbose=False,
                   approve=lambda n, a: asked.append(n) or False)
    out = {m["tool_call_id"]: m["content"] for m in run.history if m["role"] == "tool"}
    assert "revise chapter 3" in out["r"]  # read over MCP
    assert out["u"] == "the graduation project"  # asked locally
    assert "did not approve" in out["w"] and not (vault / "plan.md").exists()  # the write asked, and was refused
    assert "outside the vault" in out["e"] and asked == ["write_file"]  # the escape was denied without asking


def test_ask_over_stdio_with_sa_tools_mcp(vault, monkeypatch, capsys):
    """Phase 4 acceptance: `SA_TOOLS=mcp uv run main.py ask "what's in my memory?"` (scripted model, real
    subprocess server over stdio)."""
    monkeypatch.setenv("SA_TOOLS", "mcp")
    monkeypatch.setenv("SA_MEMORY", "tool")  # make the model fetch MEMORY.MD through the MCP server
    fake = FakeLLM(use_tools(tool_call("read_file", {"file_name": "MEMORY.MD"})), answer("You need to revise ch. 3."))
    monkeypatch.setattr(llm, "chat", fake)
    assert main.main(["ask", "What's in my memory?"]) == 0
    tool_result = next(m["content"] for m in fake.requests[1]["messages"] if m["role"] == "tool")
    assert '<file path="MEMORY.MD"' in tool_result and "revise chapter 3" in tool_result
    assert "Vault tools over MCP" in capsys.readouterr().out


def test_open_tools_is_local_by_default(vault):
    with open_tools(config.load()) as tools:
        assert isinstance(tools, Tools)
