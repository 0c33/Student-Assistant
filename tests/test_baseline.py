import re
from pathlib import Path

import pytest

from tests.fake_llm import FakeLLM, answer, tool_call, use_tools

PROJECT = Path(__file__).resolve().parent.parent

LAN_IP = re.compile(r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b")
HOME_PATH = re.compile(r"(?<![\w>.])/home/\w+|/Users/\w+|[A-Z]:\\Users\\")


def shipped_files():
    """Code and config that end up in the repo (tests are excluded: they contain the patterns above)."""
    files = [PROJECT / "main.py", PROJECT / ".env.example", PROJECT / "pyproject.toml"]
    for folder in ("sa", "servers", "evals", "security"):
        files += sorted((PROJECT / folder).rglob("*.py"))
    return [f for f in files if f.exists()]


@pytest.mark.parametrize("path", shipped_files(), ids=lambda p: str(p.relative_to(PROJECT)))
def test_no_lan_ips_or_home_paths(path):
    text = path.read_text(encoding="utf-8")
    assert not LAN_IP.search(text), f"LAN IP in {path.name}: {LAN_IP.search(text).group()}"
    assert not HOME_PATH.search(text), f"absolute home path in {path.name}: {HOME_PATH.search(text).group()}"


def test_fake_llm_replies_in_order_and_records_requests():
    call = tool_call("read_file", {"file_name": "MEMORY.MD"}, id="call_a")
    llm = FakeLLM(use_tools(call), answer("Done."))
    messages = [{"role": "user", "content": "hi"}]

    first = llm(messages, tools=[{"type": "function"}])
    messages.append(first)
    second = llm(messages)

    assert first["tool_calls"][0]["id"] == "call_a"
    assert first["tool_calls"][0]["function"]["arguments"] == '{"file_name": "MEMORY.MD"}'
    assert second == {"role": "assistant", "content": "Done."}
    assert len(llm.requests[0]["messages"]) == 1, "requests are copied at call time, not aliased"
    assert len(llm.requests[1]["messages"]) == 2


def test_fake_llm_raises_scripted_errors_and_when_exhausted():
    llm = FakeLLM(RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        llm([])
    with pytest.raises(AssertionError, match="exhausted"):
        llm([])


def test_main_refuses_a_missing_custom_vault(isolated_env, monkeypatch, capsys):
    """A typo in SA_ROOT must not create folders somewhere on the disk."""
    import main

    missing = isolated_env.parent / "typo" / "vault"
    monkeypatch.setenv("SA_ROOT", str(missing))
    assert main.main(["ask", "hi"]) == 2
    assert not missing.exists()
    assert "not a folder" in capsys.readouterr().out
