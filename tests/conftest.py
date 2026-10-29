import os

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test starts from the code's defaults (no SA_* setting from a developer's .env), with its own vault and
    state dir, no response cache and a fake key: never Sami's real vault, never the free-model quota."""
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    root, state = tmp_path / "vault", tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("SA_ROOT", str(root))
    monkeypatch.setenv("SA_STATE", str(state))
    monkeypatch.setenv("SA_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    monkeypatch.setenv("SA_CACHE", "0")
    return root
