"""The eval machinery itself: graders, the task file, one full trial with a scripted model, the report."""

import csv
import json
import shutil

import pytest

from evals import graders, report, run
from sa import config, llm
from tests.fake_llm import FakeLLM, answer, tool_call, use_tools

FIXTURE = run.FIXTURE


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "after"
    shutil.copytree(FIXTURE, v)
    return v


def grade(graders_, vault, final="", calls=(), chat=None):
    return graders.grade({"prompt": "p", "graders": graders_}, final, list(calls), vault, FIXTURE, chat)


def test_the_task_file_meets_the_capstone_bar():
    tasks = [json.loads(line) for line in run.TASKS.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(tasks) >= 15 and len({t["id"] for t in tasks}) == len(tasks)
    assert {t["lang"] for t in tasks} == {"en", "ar"}
    assert len({t["category"] for t in tasks}) >= 6
    for t in tasks:  # every grader is a known type and every file it names exists in the fixture
        for g in t["graders"]:
            assert g["type"] in {"contains_any", "not_contains", "tool_called", "tool_not_called", "file_exists",
                                 "file_contains", "file_not_contains", "file_unchanged", "file_changed", "new_file",
                                 "judge"}
            if g["type"] in ("file_contains", "file_not_contains", "file_unchanged", "file_changed"):
                assert (FIXTURE / g["path"]).is_file(), (t["id"], g["path"])


def test_answer_graders_read_arabic_digits_and_ignore_case(vault):
    out = grade([{"type": "contains_any", "values": ["22"]}, {"type": "not_contains", "values": ["done writing"]}],
                vault, final="باقي ٢٢ يوم")
    assert [g["pass"] for g in out] == [True, True]
    assert not grade([{"type": "not_contains", "values": ["Done writing"]}], vault, final="DONE WRITING")[0]["pass"]


def test_tool_graders(vault):
    calls = [("read_file", {}), ("user_input", {})]
    out = grade([{"type": "tool_called", "name": "user_input"}, {"type": "tool_not_called", "name": "write_file"},
                 {"type": "tool_called", "name": "make_dir"}], vault, calls=calls)
    assert [g["pass"] for g in out] == [True, True, False]


def test_file_graders(vault):
    (vault / "MEMORY.MD").write_text((FIXTURE / "MEMORY.MD").read_text(encoding="utf-8") + "- Data Visualization\n",
                                     encoding="utf-8")
    (vault / "projects" / "cloud-paper").mkdir()
    (vault / "projects" / "cloud-paper" / "README.md").write_text("# Cloud Computing paper\n## TODO\n", encoding="utf-8")
    out = grade([{"type": "file_contains", "path": "MEMORY.MD", "values": ["data visualization", "Whisper"]},
                 {"type": "file_not_contains", "path": "MEMORY.MD", "values": ["pwned"]},
                 {"type": "file_changed", "path": "MEMORY.MD"},
                 {"type": "file_unchanged", "path": "Data.md"},
                 {"type": "file_exists", "path": "projects/cloud-paper/README.md"},
                 {"type": "new_file", "under": "projects/", "values": ["cloud"]}], vault)
    assert all(g["pass"] for g in out), out
    out = grade([{"type": "file_unchanged", "path": "MEMORY.MD"}, {"type": "new_file", "under": "notes/", "values": []},
                 {"type": "file_contains", "path": "nope.md", "values": ["x"]}], vault)
    assert [g["pass"] for g in out] == [False, False, False]


def test_judge_parses_verdicts_and_fails_safe(vault):
    ok = FakeLLM(answer('Sure. {"pass": true, "reason": "replies in Arabic"}'))
    assert grade([{"type": "judge", "rubric": "Replies in Arabic."}], vault, "مرحبا", chat=ok)[0]["pass"]
    assert "Replies in Arabic." in ok.requests[0]["messages"][1]["content"]
    bad = FakeLLM(answer("I think it passes"))
    assert grade([{"type": "judge", "rubric": "r"}], vault, "x", chat=bad)[0]["detail"].startswith("unparseable")
    down = FakeLLM(llm.LLMError("down"))
    assert not grade([{"type": "judge", "rubric": "r"}], vault, "x", chat=down)[0]["pass"]


def test_scripted_user_input_runs_out_with_an_error():
    ask = run.scripted(["The graduation project."])
    assert ask("Which project?") == "The graduation project."
    with pytest.raises(RuntimeError, match="no scripted answer left"):
        ask("And then?")


def test_one_full_trial_with_a_scripted_model(tmp_path, monkeypatch):
    """run_trial: fresh vault copy, the agent, graders, transcript, CSV row; the fixture itself is untouched."""
    fake = FakeLLM(use_tools(tool_call("replace_in_file", {
        "file_name": "MEMORY.MD", "old_content": "- 2026-10-25: Took IoT Systems as the elective.",
        "new_content": "- 2026-10-29: Dropped IoT Systems; took Data Visualization instead."})),
        answer("Noted: Data Visualization instead of IoT."))
    monkeypatch.setattr(llm, "chat", fake)
    task = {"id": "mem-new-decision", "category": "memory", "lang": "en",
            "prompt": "I decided to drop the IoT elective and take Data Visualization instead. Remember that.",
            "graders": [{"type": "file_contains", "path": "MEMORY.MD", "values": ["Data Visualization"]}]}
    before = (FIXTURE / "MEMORY.MD").read_text(encoding="utf-8")

    row = run.run_trial(task, "B", 0, config.load(), tmp_path)
    assert row["passed"] and row["stopped"] == "done" and row["tools"] == "replace_in_file" and row["steps"] == 2
    assert (FIXTURE / "MEMORY.MD").read_text(encoding="utf-8") == before
    transcript = json.loads((tmp_path / "transcripts" / "B" / "mem-new-decision-0.json").read_text(encoding="utf-8"))
    assert "Data Visualization" in transcript["files_changed_or_new"]["MEMORY.MD"]
    assert transcript["messages"][0] == {"role": "user", "content": task["prompt"]}
    assert "## Recent Decisions" in fake.requests[0]["messages"][0]["content"]  # config B: memory in the prompt


def test_report_tables_and_readme_markers(tmp_path, monkeypatch):
    rows = {"A": [("mem-recall", "memory", [False, True, False]), ("date-ai-final", "dates", [True, True, True])],
            "B": [("mem-recall", "memory", [True, True, True]), ("date-ai-final", "dates", [True, False, True])]}
    for name, tasks in rows.items():
        with (tmp_path / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=run.COLUMNS)
            w.writeheader()
            for task, cat, results in tasks:
                for k, ok in enumerate(results):
                    w.writerow({"config": name, "model": "m", "task": task, "category": cat, "lang": "en",
                                "trial": k, "passed": ok, "failed_graders": "", "tools": "", "steps": 2,
                                "tokens": 1000, "seconds": 1, "stopped": "done"})
    (tmp_path / "root_causes.csv").write_text("config,task,trial,root_cause,note,fix\n"
                                              "A,mem-recall,0,missing tool call,did not read MEMORY.MD,config B\n",
                                              encoding="utf-8")
    text = report.report(tmp_path)
    assert "| A | read with `read_file` (spec instruction) | 6 | 0.67 | 1.00 | 0.50 | 2.0 | 1,000 |" in text
    assert "| `mem-recall` | 1/3 | 3/3 | B better |" in text and "| `date-ai-final` | 3/3 | 2/3 | A better |" in text
    assert "| missing tool call | 1 | 0 | `mem-recall` | config B |" in text
    assert "| unlabelled | 1 | 1 |" in text

    readme = tmp_path / "README.md"
    readme.write_text(f"# x\n{report.START}\nold\n{report.END}\ntail\n", encoding="utf-8")
    monkeypatch.setattr(report, "README", readme)
    assert report.update_readme("NEW")
    assert readme.read_text(encoding="utf-8") == f"# x\n{report.START}\nNEW\n{report.END}\ntail\n"
