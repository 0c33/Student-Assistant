"""The inner loop with a scripted model: message order, errors as JSON, budgets, the permission pipeline."""

import json

import pytest

from sa import llm
from sa.harness import run_task
from sa.llm import LLMError
from sa.tools import SCHEMAS, Tools
from tests.fake_llm import FakeLLM, answer, tool_call, use_tools


@pytest.fixture
def tools(isolated_env):
    (isolated_env / "MEMORY.MD").write_text("## Active Context\n- AI final exam on 2026-11-20\n", encoding="utf-8")
    return Tools(isolated_env, ask=lambda question: "the graduation project")


def run(llm_, tools, task="What's active?", **kw):
    return run_task(task, [], tools, system="SYSTEM", chat=llm_, verbose=False, **kw)


def roles(messages):
    return [m["role"] for m in messages]


def test_read_then_answer_gives_the_full_history(tools):
    """Phase 1 acceptance: system, user, assistant(tool_calls), tool, assistant."""
    fake = FakeLLM(use_tools(tool_call("read_file", {"file_name": "MEMORY.MD"}, id="call_1")),
                   answer("Your AI final is on 20 November."))
    r = run(fake, tools)

    assert roles(r.history) == ["system", "user", "assistant", "tool", "assistant"]
    assert r.history[2]["tool_calls"][0]["id"] == "call_1"
    assert r.history[3]["tool_call_id"] == "call_1"
    assert "AI final exam" in r.history[3]["content"]
    assert (r.final, r.stopped, r.steps) == ("Your AI final is on 20 November.", "done", 2)
    assert r.tool_calls == [("read_file", {"file_name": "MEMORY.MD"})]


def test_the_model_sees_its_own_tool_call_before_the_result(tools):
    """Bug 1: the second request must carry the assistant tool_calls message, or the tool_call_id dangles (400)."""
    fake = FakeLLM(use_tools(tool_call("read_file", {"file_name": "MEMORY.MD"}, id="call_1")), answer("ok"))
    run(fake, tools)

    second = fake.requests[1]["messages"]
    assert roles(second) == ["system", "user", "assistant", "tool"]
    assert second[2]["tool_calls"][0]["id"] == second[3]["tool_call_id"] == "call_1"


def test_only_implemented_tools_are_advertised(tools):
    """Bug 2: no sync_obsidian_graph / graph_status, and every schema has a function behind it."""
    fake = FakeLLM(answer("ok"))
    run(fake, tools)
    advertised = [t["function"]["name"] for t in fake.requests[0]["tools"]]
    assert advertised == list(tools.registry) == list(SCHEMAS)
    assert "sync_obsidian_graph" not in advertised and "graph_status" not in advertised


@pytest.mark.parametrize("calls", [None, []])
def test_null_or_empty_tool_calls_end_the_task(tools, calls):
    """Bug 4: servers send `null` or `[]` for "no tool calls"."""
    r = run(FakeLLM({"role": "assistant", "content": "Done.", "tool_calls": calls}), tools)
    assert (r.stopped, r.steps, r.final) == ("done", 1, "Done.")
    assert "tool_calls" not in r.history[-1]


def test_tool_failures_come_back_as_json_and_the_loop_goes_on(tools):
    """Bug 5: bad JSON, unknown tool, wrong kwargs, missing paths: errors the model can read, never a crash."""
    fake = FakeLLM(use_tools(tool_call("read_file", "{oops", id="bad_json"),
                             tool_call("graph_status", {}, id="unknown"),
                             tool_call("read_file", {"path": "MEMORY.MD"}, id="wrong_kwargs"),
                             tool_call("list_dir", {"directory": "no/such/folder"}, id="missing_dir"),
                             tool_call("read_file", {"file_name": "nope.md"}, id="missing_file")),
                   answer("Recovered."))
    r = run(fake, tools)

    results = {m["tool_call_id"]: json.loads(m["content"]) for m in r.history if m["role"] == "tool"}
    assert set(results) == {"bad_json", "unknown", "wrong_kwargs", "missing_dir", "missing_file"}
    assert all("error" in out for out in results.values())
    assert "not a valid JSON object" in results["bad_json"]["error"]
    assert "Unknown tool 'graph_status'" in results["unknown"]["error"]
    assert "Bad arguments for read_file" in results["wrong_kwargs"]["error"]
    assert (r.stopped, r.final) == ("done", "Recovered.")


def test_provider_only_fields_are_not_stored_or_sent_back(tools):
    fake = FakeLLM({**use_tools(tool_call("list_dir", {})), "reasoning_content": "hmm", "refusal": None},
                   {**answer("ok"), "reasoning_content": "done thinking"})
    r = run(fake, tools)
    for m in r.history:
        if m["role"] == "assistant":
            assert set(m) <= {"role", "content", "tool_calls"}
    assert "reasoning_content" not in fake.requests[1]["messages"][2]


def test_step_budget_stops_with_a_well_formed_history(tools):
    fake = FakeLLM(*[use_tools(tool_call("list_dir", {})) for _ in range(3)])
    r = run(fake, tools, max_steps=3)

    assert (r.stopped, r.steps, r.final) == ("max_steps", 3, "")
    assert r.history[-1]["role"] == "assistant" and "[harness]" in r.history[-1]["content"]
    assert roles(r.history)[-2] == "tool"  # the last tool result still follows its call


def test_cost_budget_stops_the_task(tools):
    ledger = llm.Ledger()

    def pricey(messages, tools_, model=None):
        ledger.cost += 0.03
        return use_tools(tool_call("list_dir", {}))

    r = run_task("loop", [], tools, system="S", chat=pricey, max_cost=0.05, ledger=ledger, verbose=False)
    assert (r.stopped, r.steps) == ("max_cost", 2)
    assert r.cost == pytest.approx(0.06)


def test_model_error_stops_the_task_cleanly(tools):
    fake = FakeLLM(use_tools(tool_call("list_dir", {})), LLMError("No model answered."))
    r = run(fake, tools)
    assert (r.stopped, r.final) == ("error", "")
    assert roles(r.history) == ["system", "user", "assistant", "tool", "assistant"]
    assert "No model answered" in r.history[-1]["content"]


def test_history_is_sent_between_system_and_task(tools):
    earlier = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    fake = FakeLLM(answer("ok"))
    run_task("next", earlier, tools, system="S", chat=fake, verbose=False)
    assert fake.requests[0]["messages"] == [{"role": "system", "content": "S"}, *earlier,
                                            {"role": "user", "content": "next"}]


def test_permission_pipeline(tools, isolated_env):
    """parse → permission → (approve) → before_tool → execute → after_tool, and deny fails closed."""
    seen = []

    def permission(name, args):
        seen.append(("permission", name))
        return {"write_file": "deny", "make_dir": "ask", "user_input": "maybe"}.get(name, "allow")

    def approve(name, args):
        seen.append(("approve", name))
        return False

    def before_tool(name, args):
        seen.append(("before", name))
        return '{"error": "blocked by policy"}' if name == "read_file" else None

    def after_tool(name, args, result):
        seen.append(("after", name))
        return result.upper() if name == "list_dir" else None

    fake = FakeLLM(use_tools(tool_call("write_file", {"file_name": "x.md", "content": "x"}, id="w"),
                             tool_call("make_dir", {"directory_path": "projects"}, id="m"),
                             tool_call("user_input", {"question": "?"}, id="u"),
                             tool_call("read_file", {"file_name": "MEMORY.MD"}, id="r"),
                             tool_call("list_dir", {}, id="l")),
                   answer("ok"))
    r = run(fake, tools, permission=permission, approve=approve, before_tool=before_tool, after_tool=after_tool)
    out = {m["tool_call_id"]: m["content"] for m in r.history if m["role"] == "tool"}

    assert "denied write_file" in out["w"] and not (isolated_env / "x.md").exists()
    assert "denied make_dir" in out["m"] and not (isolated_env / "projects").exists()  # approval refused
    assert "denied user_input" in out["u"]  # unknown decision → deny
    assert out["r"] == '{"error": "blocked by policy"}'
    assert out["l"] == '["MEMORY.MD"]'  # after_tool replaced the result
    assert seen == [("permission", "write_file"), ("permission", "make_dir"), ("approve", "make_dir"),
                    ("permission", "user_input"), ("permission", "read_file"), ("before", "read_file"),
                    ("after", "read_file"), ("permission", "list_dir"), ("before", "list_dir"),
                    ("after", "list_dir")]


def test_approved_calls_run(tools, isolated_env):
    fake = FakeLLM(use_tools(tool_call("make_dir", {"directory_path": "projects/gp"})), answer("ok"))
    run(fake, tools, permission=lambda n, a: "ask", approve=lambda n, a: True)
    assert (isolated_env / "projects" / "gp").is_dir()


def test_user_input_is_answered_by_the_ask_hook(tools):
    fake = FakeLLM(use_tools(tool_call("user_input", {"question": "Which project?"}, id="q")), answer("ok"))
    r = run(fake, tools)
    assert r.history[3]["content"] == "the graduation project"


def test_tools_read_and_write_utf8(tools, isolated_env):
    fake = FakeLLM(use_tools(tool_call("write_file", {"file_name": "ملاحظات.md", "content": "اختبار الذكاء الاصطناعي",
                                                      "write_mode": "w"})),
                   use_tools(tool_call("read_file", {"file_name": "ملاحظات.md"}, id="read")),
                   answer("ok"))
    r = run(fake, tools)
    assert (isolated_env / "ملاحظات.md").read_text(encoding="utf-8") == "اختبار الذكاء الاصطناعي"
    assert "اختبار الذكاء الاصطناعي" in next(m["content"] for m in r.history if m.get("tool_call_id") == "read")
