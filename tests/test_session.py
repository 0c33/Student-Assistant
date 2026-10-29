"""The outer loop: summaries, the raw tail, JSONL persistence and resume, /new and /quit."""

import json

import pytest

from sa import config
from sa.llm import LLMError
from sa.session import Session, chat_loop, parse_summary, summarize, tail
from tests.fake_llm import FakeLLM, answer, tool_call, use_tools

SUMMARY = answer('{"user_input": "asked what is in Data.md", "agent_response": "read Data.md: AI final on 20 Nov"}')


def user(text="q"):
    return {"role": "user", "content": text}


def final(text="a"):
    return {"role": "assistant", "content": text}


def with_tools(n, prefix):
    """A task with n tool round trips: user, (assistant(tool_calls), tool) × n, final."""
    msgs = [user(prefix)]
    for i in range(n):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"{prefix}{i}", "type": "function",
                                     "function": {"name": "list_dir", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"{prefix}{i}", "content": "[]"})
    return msgs + [final(prefix)]


def well_formed(messages):
    """Starts on a user message, and every tool result follows the call that produced it."""
    if messages and messages[0]["role"] != "user":
        return False
    open_ids = set()
    for m in messages:
        if m["role"] == "assistant":
            open_ids = {c["id"] for c in m.get("tool_calls") or []}
        elif m["role"] == "tool" and m["tool_call_id"] not in open_ids:
            return False
    return True


def test_tail_keeps_whole_tasks_newest_first():
    t1, t2, t3 = with_tools(1, "a"), [user("b"), final("b")], with_tools(2, "c")  # 4, 2, 6 messages
    assert tail([t1, t2, t3], 8) == [t2, t3]
    assert tail([t1, t2, t3], 12) == [t1, t2, t3]
    assert tail([t1, t2, t3], 7) == [t3]
    assert tail([t1, t2, t3], 5) == [[t3[0], t3[-1]]]  # too long alone: question and final answer only
    assert tail([], 5) == []


@pytest.mark.parametrize("budget", range(2, 16))
def test_tail_never_orphans_a_tool_result(budget):
    turns = [with_tools(1, "a"), [user("b"), final("b")], with_tools(3, "c"), with_tools(2, "d")]
    kept = [m for turn in tail(turns, budget) for m in turn]
    assert kept and well_formed(kept)
    assert len(kept) <= max(budget, 2)


def test_session_runs_tasks_summarises_and_persists(isolated_env):
    (isolated_env / "Data.md").write_text("## Exams\n- AI final: 20 November 2026\n", encoding="utf-8")
    cfg = config.load()
    fake = FakeLLM(use_tools(tool_call("read_file", {"file_name": "Data.md"}, id="call_1")),
                   answer("Your AI final is on 20 November."),
                   SUMMARY,  # task 1 used a tool → the small model summarises it
                   answer("Hi Sami!"))  # task 2 is short, no tools → summarised without a model call
    s = Session(cfg, "test", chat=fake, verbose=False)

    r1 = s.ask("What's in my data?")
    r2 = s.ask("hello")

    assert (r1.final, r2.final) == ("Your AI final is on 20 November.", "Hi Sami!")
    assert len(fake.requests) == 4
    summary_request = fake.requests[2]
    assert summary_request["model"] == cfg.small_model and summary_request["tools"] is None
    assert s.summaries == ["Sami: asked what is in Data.md → Assistant: read Data.md: AI final on 20 Nov",
                           "Sami: hello → Assistant: Hi Sami!"]

    task2 = fake.requests[3]["messages"]
    assert "AI final: 20 November 2026" in task2[0]["content"]  # Data.md from the vault
    assert "EARLIER TASKS" in task2[0]["content"] and "read Data.md" in task2[0]["content"]
    assert [m["role"] for m in task2] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert well_formed(task2[1:])

    events = [json.loads(line) for line in s.path.read_text(encoding="utf-8").splitlines()]
    assert [e["type"] for e in events] == ["turn", "summary", "turn", "summary"]
    assert events[0]["messages"][0] == {"role": "user", "content": "What's in my data?"}
    assert events[0]["stopped"] == "done" and "ts" in events[0]


def test_resume_rebuilds_the_same_context(isolated_env):
    cfg = config.load()
    s = Session(cfg, "resume", chat=FakeLLM(answer("one"), answer("two")), verbose=False)
    s.ask("first")
    s.ask("second")

    again = Session(cfg, "resume", chat=FakeLLM(), verbose=False)
    assert again.turns == s.turns
    assert again.summaries == s.summaries
    assert again.system_prompt() == s.system_prompt()


def test_new_drops_the_raw_tail_but_keeps_summaries(isolated_env):
    cfg = config.load()
    fake = FakeLLM(answer("one"), answer("two"))
    s = Session(cfg, "fresh", chat=fake, verbose=False)
    s.ask("first")
    s.new()
    s.ask("second")

    assert [m["role"] for m in fake.requests[1]["messages"]] == ["system", "user"]  # no raw messages after /new
    assert "first" in fake.requests[1]["messages"][0]["content"]  # but the summary is there
    again = Session(cfg, "fresh", chat=FakeLLM(), verbose=False)
    assert again.turns == [[{"role": "user", "content": "second"}, {"role": "assistant", "content": "two"}]]
    assert len(again.summaries) == 2


def test_history_is_bounded(isolated_env, monkeypatch):
    """Bug 6: history no longer grows forever."""
    monkeypatch.setenv("SA_KEEP_MESSAGES", "4")
    monkeypatch.setenv("SA_KEEP_SUMMARIES", "2")
    s = Session(config.load(), chat=FakeLLM(*[answer(f"a{i}") for i in range(5)]), verbose=False)
    for i in range(5):
        s.ask(f"q{i}")
    assert sum(map(len, s.turns)) <= 4 and len(s.summaries) == 2
    assert s.path is None  # unnamed sessions are not saved


def test_summary_falls_back_when_the_model_fails():
    turn = with_tools(1, "a")
    assert summarize(turn, FakeLLM(LLMError("down")), "small") == "Sami: a → Assistant: a"
    assert summarize(turn, FakeLLM(answer("Sure! Here is a summary.")), "small") == "Sami: a → Assistant: a"


def test_parse_summary_validates_the_json_shape():
    s = parse_summary('```json\n{"user_input": "u", "agent_response": "r"}\n```')
    assert (s.user_input, s.agent_response) == ("u", "r")
    with pytest.raises(ValueError):
        parse_summary('{"user_input": "u"}')  # agent_response missing
    with pytest.raises(ValueError):
        parse_summary("no json here")


def test_missing_data_md_is_stated_not_hidden(isolated_env):
    """Bug 10 (part): a missing Data.md is said plainly instead of injecting an error string."""
    prompt = Session(config.load(), verbose=False).system_prompt()
    assert "Data.md was not found in the vault" in prompt


def test_chat_loop_commands(isolated_env, capsys):
    cfg = config.load()
    s = Session(cfg, "loop", chat=FakeLLM(answer("Hello!")), verbose=False)
    inputs = iter(["hi", "/help", "/new", ""])
    chat_loop(s, read=lambda prompt: next(inputs))

    out = capsys.readouterr().out
    assert "🤖 Hello!" in out and "Commands:" in out and "Fresh start" in out and "Bye" in out
    types = [json.loads(line)["type"] for line in s.path.read_text(encoding="utf-8").splitlines()]
    assert types == ["turn", "summary", "new"]


@pytest.mark.parametrize("ending", ["/quit", EOFError])
def test_chat_loop_exits_on_quit_and_eof(isolated_env, ending):
    def read(prompt):
        if ending is EOFError:
            raise EOFError
        return ending

    chat_loop(Session(config.load(), chat=FakeLLM(), verbose=False), read=read)  # FakeLLM() would fail if called
