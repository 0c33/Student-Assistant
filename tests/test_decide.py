"""The clarify gate on Sami's decision model: a hint when the request is ambiguous, nothing otherwise."""

import pytest

from sa import config, decide
from sa.session import Session
from tests.fake_llm import FakeLLM, answer


class Reply:
    def __init__(self, p_ask):
        self.p = p_ask

    def raise_for_status(self):
        pass

    def json(self):
        return {"answers": {"next_action": {"type": "choice", "choice": "ask_more" if self.p > 0.5 else "proceed",
                                            "probabilities": {"proceed": 1 - self.p, "ask_more": self.p}}}}


@pytest.fixture
def service(monkeypatch):
    sent = []

    def install(p_ask=None, error=None):
        def post(url, json, timeout):
            sent.append({"url": url, "json": json})
            if error:
                raise error
            return Reply(p_ask)
        monkeypatch.setattr(decide.requests, "post", post)
        monkeypatch.setenv("SA_DECISION_URL", "http://decision.test/v1/systemone")
        return sent
    return install


def test_the_service_gets_the_request_and_memory(service, isolated_env):
    (isolated_env / "MEMORY.MD").write_text("- two projects: Saut and a portfolio site", encoding="utf-8")
    sent = service(p_ask=0.65)
    Session(config.load(), chat=FakeLLM(answer("Which project?")), verbose=False).ask("Make a plan for the project.")
    assert sent[0]["url"] == "http://decision.test/v1/systemone"
    assert set(sent[0]["json"]["questions"]["next_action"]["options"]) == {"proceed", "ask_more"}
    assert "Make a plan for the project." in sent[0]["json"]["state"] and "portfolio" in sent[0]["json"]["state"]


def test_hint_reaches_the_model_only_above_the_threshold(service, isolated_env):
    for p, expected in [(0.65, True), (0.55, False)]:
        service(p_ask=p)
        fake = FakeLLM(answer("ok"))
        Session(config.load(), chat=fake, verbose=False).ask("Make a plan for the project.")
        assert ("HARNESS NOTE" in fake.requests[0]["messages"][0]["content"]) is expected


def test_no_url_no_gate_and_failures_are_harmless(service, isolated_env, monkeypatch, capsys):
    monkeypatch.delenv("SA_DECISION_URL", raising=False)
    assert decide.clarify_hint(config.load(), "x", "") is None
    service(error=ConnectionError("down"))
    fake = FakeLLM(answer("ok"))
    Session(config.load(), chat=fake, verbose=False).ask("Make a plan.")
    assert "HARNESS NOTE" not in fake.requests[0]["messages"][0]["content"]
    assert "decision model unavailable" in capsys.readouterr().out
