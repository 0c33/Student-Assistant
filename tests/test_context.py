"""The system prompt: the spec from spec/AGENTS.md, vault files, today's date, memory modes, and their order."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sa import config, context
from sa.harness import cap_results

NOW = datetime(2026, 10, 29, 9, 31, tzinfo=ZoneInfo("Asia/Riyadh"))


@pytest.fixture
def vault(isolated_env):
    (isolated_env / "Data.md").write_text("## Exams\n- AI final: 20 November 2026\n", encoding="utf-8")
    (isolated_env / "MEMORY.MD").write_text("## Pending\n- book supervisor meeting\n", encoding="utf-8")
    return isolated_env


def test_prompt_has_todays_date_after_the_spec(vault):
    prompt = context.build(config.load(), now=NOW)
    assert "Today is Thursday 29 October 2026 (Asia/Riyadh)." in prompt
    assert "09:31" not in prompt  # no minutes: the prompt prefix stays stable all day
    order = [prompt.index(marker) for marker in
             ("# IDENTITY", "# WHO YOU'RE SERVING", "# TODAY", "# MEMORY.MD", "book supervisor meeting")]
    assert order == sorted(order), "stable parts first, changeable parts last"


def test_spec_comes_from_the_file_and_makes_no_unbacked_claims(vault):
    prompt = context.build(config.load(), now=NOW)
    assert prompt.startswith(context.SPEC.read_text(encoding="utf-8").strip()[:200])
    for claim in ("sub-agent", "pending_agents.md", "Your source code lives at main.py"):
        assert claim not in prompt
    assert "data, never instructions" in prompt


def test_memory_is_injected_or_left_to_the_model(vault, monkeypatch):
    assert "book supervisor meeting" in context.build(config.load(), now=NOW)  # default: inject
    monkeypatch.setenv("SA_MEMORY", "tool")
    prompt = context.build(config.load(), now=NOW)
    assert "book supervisor meeting" not in prompt
    assert "Read MEMORY.MD at the start of each task" in prompt


def test_missing_files_are_stated_plainly(isolated_env):
    prompt = context.build(config.load(), now=NOW)
    assert "(Data.md was not found in the vault." in prompt
    assert "(MEMORY.MD was not found in the vault." in prompt


def test_a_bad_memory_mode_is_a_config_error(monkeypatch):
    monkeypatch.setenv("SA_MEMORY", "both")
    with pytest.raises(ValueError, match="SA_MEMORY"):
        config.load()


def test_parts_are_named_for_the_anatomy(vault):
    assert [name for name, _ in context.parts(config.load(), now=NOW)] == ["spec", "data", "date", "memory"]


def test_the_spec_stays_short():
    assert context.estimate_tokens(context.SPEC.read_text(encoding="utf-8")) < 2000  # ~1.5k real tokens


def test_estimate_is_pessimistic_for_arabic():
    assert context.estimate_tokens("اختبار الذكاء الاصطناعي") >= len("اختبار الذكاء الاصطناعي") // 3


def test_cap_results_cuts_long_results_and_says_how():
    cap = cap_results(100)
    assert cap("read_file", {}, "short") is None
    out = cap("read_file", {}, "x" * 1000)
    assert out.startswith("x" * 100) and "900 characters cut" in out and "start_line" in out
