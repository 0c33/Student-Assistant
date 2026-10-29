"""Harness control as shipped: the default policy, approvals, guards that hold after approval, the audit log,
traces and budgets, driven through the real entry points (main.main, Session) with a scripted model."""

import json

import pytest

import main
from sa import config, llm
from sa.harness import Policy, auto_approve, preview, run_task
from sa.session import Session
from sa.telemetry import redact
from sa.tools import Tools, make_tools
from tests.fake_llm import FakeLLM, answer, tool_call, use_tools

WRITE = tool_call("write_file", {"file_name": "projects/plan.md", "content": "# Plan\n- step 1\n"}, id="w")


@pytest.fixture
def vault(isolated_env):
    (isolated_env / "projects").mkdir()
    (isolated_env / "MEMORY.MD").write_text("## Pending\n- revise chapter 3\n", encoding="utf-8")
    return isolated_env


@pytest.fixture
def model(monkeypatch):
    """Replace the real model everywhere (llm.chat is looked up at call time), and give the run its own ledger."""
    def install(*script):
        fake = FakeLLM(*script)
        monkeypatch.setattr(llm, "chat", fake)
        monkeypatch.setattr(llm, "LEDGER", llm.Ledger())
        return fake
    return install


@pytest.fixture
def answers(monkeypatch):
    """Scripted terminal input; records every prompt shown."""
    def install(*replies):
        replies, prompts = list(replies), []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return replies.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)
        return prompts
    return install


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- the policy
def test_policy_reads_run_writes_ask_escapes_are_denied(vault):
    policy = Policy(make_tools(config.load()))
    assert policy("read_file", {"file_name": "MEMORY.MD"}) == "allow"
    assert policy("list_dir", {}) == "allow"
    assert policy("user_input", {"question": "?"}) == "allow"
    for name in ("write_file", "replace_in_file", "remove_from_file", "make_dir"):
        assert policy(name, {"file_name": "projects/x.md", "directory_path": "projects/x"}) == "ask"
    assert policy("write_file", {"file_name": "../../.bashrc", "content": "x"}).startswith("deny: ")
    assert policy("read_file", {"file_name": "/etc/passwd"}).startswith("deny: ")


def test_preview_shows_what_a_write_changes(vault):
    t = Tools(vault)
    p = preview(t, "replace_in_file", {"file_name": "MEMORY.MD", "old_content": "- revise chapter 3",
                                       "new_content": "- revise chapters 3 and 4"})
    assert p.splitlines() == ["🛡️  replace_in_file → MEMORY.MD", "   - - revise chapter 3",
                              "   + - revise chapters 3 and 4"]
    assert "(OVERWRITES the whole file)" in preview(t, "write_file", {"file_name": "MEMORY.MD", "content": "",
                                                                      "write_mode": "w"})
    assert "(new file)" in preview(t, "write_file", {"file_name": "projects/new.md", "content": "x"})
    long = preview(t, "write_file", {"file_name": "a.md", "content": "\n".join(map(str, range(40)))})
    assert "… 28 more lines" in long


# ---------------------------------------------------------------- the shipped entry points
def test_ask_asks_before_writing_and_respects_no(vault, model, answers):
    model(use_tools(WRITE), answer("I didn't write it."))
    prompts = answers("n")
    assert main.main(["ask", "Make a plan file"]) == 0
    assert prompts == ["🛡️  Allow this change? [y/N] "]
    assert not (vault / "projects" / "plan.md").exists()


def test_ask_writes_after_yes(vault, model, answers):
    model(use_tools(WRITE), answer("Written."))
    answers("y")
    main.main(["ask", "Make a plan file"])
    assert (vault / "projects" / "plan.md").read_text(encoding="utf-8") == "# Plan\n- step 1\n"


def test_yes_flag_approves_writes_but_not_escapes(vault, model, answers):
    escape = tool_call("write_file", {"file_name": "../outside.md", "content": "pwned"}, id="e")
    fake = model(use_tools(WRITE, escape), answer("ok"))
    prompts = answers()  # any input() call would fail: nothing may ask
    main.main(["ask", "--yes", "Make a plan file"])
    assert prompts == []
    assert (vault / "projects" / "plan.md").exists()
    assert not (vault.parent / "outside.md").exists()
    results = {m["tool_call_id"]: m["content"] for m in fake.requests[1]["messages"] if m["role"] == "tool"}
    assert "harness denied write_file" in results["e"] and "outside the vault" in results["e"]


def test_session_ask_goes_through_the_policy(vault, model, answers):
    model(use_tools(WRITE), answer("ok"), answer('{"user_input": "u", "agent_response": "r"}'))
    prompts = answers("n")
    Session(config.load(), "s", verbose=False).ask("Make a plan file")
    assert len(prompts) == 1 and not (vault / "projects" / "plan.md").exists()


def test_an_approved_self_edit_is_still_blocked(vault, monkeypatch, model, answers):
    """A tired human says yes; the guard in the tool still says no (Lab 3.1)."""
    (vault / "sa").mkdir()
    (vault / "sa" / "harness.py").write_text("# the loop", encoding="utf-8")
    monkeypatch.setattr("sa.tools.SELF", (vault / "sa",))  # the vault holds a copy of the agent's code
    edit = tool_call("write_file", {"file_name": "sa/harness.py", "content": "pwned", "write_mode": "w"}, id="x")
    fake = model(use_tools(edit), answer("ok"))

    tools = make_tools(config.load())
    run = run_task("Fix your harness", [], tools, system="S", permission=lambda n, a: "ask",
                   approve=lambda n, a: True, verbose=False)  # a permissive policy AND a yes
    assert (vault / "sa" / "harness.py").read_text(encoding="utf-8") == "# the loop"
    assert "assistant's own code" in run.history[3]["content"]
    assert fake.requests  # the model was asked; the tool refused


def test_allow_self_edit_flag_lifts_only_the_self_edit_guard(vault, monkeypatch, model, answers, capsys):
    (vault / "sa").mkdir()
    (vault / "sa" / "notes.md").write_text("old", encoding="utf-8")
    monkeypatch.setattr("sa.tools.SELF", (vault / "sa",))
    model(use_tools(tool_call("write_file", {"file_name": "sa/notes.md", "content": "new", "write_mode": "w"})),
          answer("ok"))
    answers("y")  # still asks
    main.main(["ask", "--allow-self-edit", "Update the notes"])
    assert (vault / "sa" / "notes.md").read_text(encoding="utf-8") == "new"
    assert "--allow-self-edit" in capsys.readouterr().out


# ---------------------------------------------------------------- audit log and traces
def test_audit_log_records_every_decision_without_personal_data(vault, model, answers):
    model(use_tools(tool_call("read_file", {"file_name": "MEMORY.MD"}),
                    tool_call("write_file", {"file_name": "projects/contact.md",
                                             "content": "Supervisor: 0551234567, ID 1048213372"}),
                    tool_call("write_file", {"file_name": "../x.md", "content": "x"})),
          answer("ok"))
    answers("n")
    main.main(["ask", "Save my supervisor's contact"])

    log = lines(config.load().state / "audit.jsonl")
    assert [(e["tool"], e["decision"], e["ok"]) for e in log] == [
        ("read_file", "allow", True), ("write_file", "refused", False), ("write_file", "denied", False)]
    assert "0551234567" not in json.dumps(log) and "1048213372" not in json.dumps(log)
    assert log[1]["args"]["content"] == "Supervisor: [REDACTED], ID [REDACTED]"
    assert all(e["ts"] and e["result_chars"] > 0 for e in log)


def test_redact_truncates_long_values():
    assert redact("x" * 500).endswith("… [300 chars cut]")
    assert redact("+966551234567 and 0551234567") == "[REDACTED] and [REDACTED]"


def test_traces_use_the_genai_conventions(vault, model, answers):
    model(use_tools(tool_call("read_file", {"file_name": "MEMORY.MD"})), answer("ok"))
    main.main(["ask", "What's pending?"])

    spans = lines(config.load().state / "traces.jsonl")
    by_op = {}
    for s in spans:
        by_op.setdefault(s["attributes"]["gen_ai.operation.name"], []).append(s)
    task = by_op["invoke_agent"][0]
    assert len(by_op["chat"]) == 2 and len(by_op["execute_tool"]) == 1
    assert {s["trace_id"] for s in spans} == {task["trace_id"]}  # one trace per task
    assert all(s["parent_id"] == task["span_id"] for s in by_op["chat"] + by_op["execute_tool"])
    tool = by_op["execute_tool"][0]["attributes"]
    assert (tool["gen_ai.tool.name"], tool["sa.decision"]) == ("read_file", "allow")
    assert task["attributes"]["sa.stopped"] == "done" and task["attributes"]["sa.steps"] == 2
    assert by_op["chat"][0]["attributes"]["gen_ai.request.model"] == config.load().model
    assert all(s["duration_ms"] >= 0 for s in spans)


# ---------------------------------------------------------------- budgets
def test_token_budget_stops_a_looping_task_on_free_models(vault):
    ledger = llm.Ledger()

    def free_but_long(messages, tools_, model=None):
        ledger.add({"prompt_tokens": 6000, "completion_tokens": 100, "cost": 0})  # free: cost stays 0
        return use_tools(tool_call("list_dir", {}))

    run = run_task("loop", [], Tools(vault), system="S", chat=free_but_long, max_steps=20, max_cost=0.05,
                   max_tokens=15000, ledger=ledger, verbose=False)
    assert (run.stopped, run.steps, run.cost) == ("max_tokens", 3, 0)
    assert run.tokens == 18300
    assert run.history[-1]["content"].startswith("[harness] Task stopped: token budget")


def test_auto_approve_says_what_it_approved(capsys):
    assert auto_approve("write_file", {}) is True
    assert "auto-approved (--yes): write_file" in capsys.readouterr().out
