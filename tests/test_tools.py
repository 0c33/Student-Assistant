"""The tools on their own: the sandbox, protected paths, unique-match edits, schemas generated from docstrings."""

import json
import os

import pytest

from sa import config
from sa.tools import SELF, Tools, make_tools, schema


@pytest.fixture
def vault(isolated_env):
    (isolated_env / "MEMORY.MD").write_text("## Pending\n- book supervisor meeting\n- revise chapter 3\n",
                                            encoding="utf-8")
    (isolated_env / "projects").mkdir()
    return isolated_env


@pytest.fixture
def tools(vault):
    return Tools(vault)


def err(result: str) -> str:
    return json.loads(result)["error"]


# ---------------------------------------------------------------- sandbox
@pytest.mark.parametrize("path", ["../../etc/passwd", "../outside.md", "projects/../../outside.md",
                                  "/etc/passwd", "/home/x/.bashrc", "~/.bashrc", "~/.ssh/id_rsa"])
def test_paths_outside_the_vault_are_refused(tools, path):
    for name, args in [("read_file", {"file_name": path}),
                       ("write_file", {"file_name": path, "content": "pwned"}),
                       ("list_dir", {"directory": path}),
                       ("make_dir", {"directory_path": path})]:
        out = tools.call(name, args)
        assert "PermissionError" in err(out), (name, path, out)
        assert tools.refusal(name, args), "the policy sees the refusal before anyone is asked"


def test_a_symlink_that_leads_out_is_refused(tools, vault, tmp_path):
    secret = tmp_path / "outside" / "secret.md"
    secret.parent.mkdir()
    secret.write_text("private", encoding="utf-8")
    os.symlink(secret, vault / "link.md")
    os.symlink(secret.parent, vault / "linkdir")

    assert "leads outside the vault" in err(tools.call("read_file", {"file_name": "link.md"}))
    assert "leads outside the vault" in err(tools.call("write_file", {"file_name": "linkdir/x.md", "content": "x"}))
    assert secret.read_text(encoding="utf-8") == "private"
    assert not (secret.parent / "x.md").exists()


def test_a_symlink_inside_the_vault_is_fine(tools, vault):
    os.symlink(vault / "MEMORY.MD", vault / "memory-link.md")
    assert "book supervisor meeting" in tools.call("read_file", {"file_name": "memory-link.md"})


def test_secrets_files_are_never_read_or_listed(tools, vault):
    (vault / ".env").write_text("OPENROUTER_API_KEY=sk-real", encoding="utf-8")
    assert "secrets file" in err(tools.call("read_file", {"file_name": ".env"}))
    assert "secrets file" in err(tools.call("write_file", {"file_name": "projects/../.env", "content": "x"}))
    assert ".env" not in tools.call("list_dir", {})


def test_protected_paths_can_be_read_but_not_written(vault):
    (vault / "sa").mkdir()
    (vault / "sa" / "harness.py").write_text("# the loop", encoding="utf-8")
    t = Tools(vault, protect=[vault / "sa"])
    assert "# the loop" in t.call("read_file", {"file_name": "sa/harness.py"})
    for name, args in [("write_file", {"file_name": "sa/harness.py", "content": "", "write_mode": "w"}),
                       ("replace_in_file", {"file_name": "sa/harness.py", "old_content": "loop", "new_content": "x"}),
                       ("make_dir", {"directory_path": "sa/new"})]:
        assert "assistant's own code" in err(t.call(name, args))
    assert (vault / "sa" / "harness.py").read_text(encoding="utf-8") == "# the loop"


def test_the_cli_protects_the_agents_own_code_and_state(monkeypatch):
    """With SA_ROOT pointing at the project itself, main.py, sa/, spec/ and the state folder stay read-only."""
    monkeypatch.setenv("SA_ROOT", str(config.PROJECT))
    cfg = config.load()
    t = make_tools(cfg)
    for path in ["main.py", "sa/harness.py", "spec/AGENTS.md"]:
        assert "assistant's own code" in t.refusal("write_file", {"file_name": path, "content": "x"})
    assert set(SELF) <= set(t.protect) and cfg.state in t.protect
    assert make_tools(cfg, allow_self_edit=True).protect == ()


def test_vault_paths_shown_to_the_model_are_relative(tools):
    assert tools.call("write_file", {"file_name": "projects/plan.md", "content": "x"}) == \
        "Done writing to file: projects/plan.md (1 characters, mode 'a')"


# ---------------------------------------------------------------- edits
def test_replace_needs_exactly_one_match(tools, vault):
    out = tools.call("replace_in_file", {"file_name": "MEMORY.MD", "old_content": "- ", "new_content": "* "})
    assert "appears 2 times" in err(out)
    assert (vault / "MEMORY.MD").read_text(encoding="utf-8").count("- ") == 2  # nothing changed

    out = tools.call("replace_in_file", {"file_name": "MEMORY.MD", "old_content": "- revise chapter 3",
                                         "new_content": "- revise chapters 3 and 4"})
    assert out.startswith("Replaced 1 occurrence")
    assert "chapters 3 and 4" in (vault / "MEMORY.MD").read_text(encoding="utf-8")


def test_replace_all_is_explicit(tools, vault):
    tools.call("replace_in_file", {"file_name": "MEMORY.MD", "old_content": "- ", "new_content": "* ",
                                   "replace_all": True})
    assert (vault / "MEMORY.MD").read_text(encoding="utf-8").count("* ") == 2


def test_empty_or_missing_text_is_refused(tools, vault):
    before = (vault / "MEMORY.MD").read_text(encoding="utf-8")
    assert "is empty" in err(tools.call("replace_in_file", {"file_name": "MEMORY.MD", "old_content": "",
                                                            "new_content": "X"}))
    assert "not found" in err(tools.call("remove_from_file", {"file_name": "MEMORY.MD",
                                                              "content_to_remove": "nothing like this"}))
    assert (vault / "MEMORY.MD").read_text(encoding="utf-8") == before


def test_remove_needs_exactly_one_match(tools, vault):
    assert "appears 2 times" in err(tools.call("remove_from_file", {"file_name": "MEMORY.MD",
                                                                     "content_to_remove": "- "}))
    tools.call("remove_from_file", {"file_name": "MEMORY.MD", "content_to_remove": "- revise chapter 3\n"})
    assert (vault / "MEMORY.MD").read_text(encoding="utf-8") == "## Pending\n- book supervisor meeting\n"


def test_list_dir_marks_folders_and_reports_missing_ones(tools):
    assert json.loads(tools.call("list_dir", {})) == ["MEMORY.MD", "projects/"]
    assert "FileNotFoundError" in err(tools.call("list_dir", {"directory": "nope"}))


# ---------------------------------------------------------------- schemas
def test_schemas_come_from_signatures_and_docstrings(tools):
    by_name = {s["function"]["name"]: s["function"] for s in tools.schemas()}
    assert list(by_name) == list(tools.registry)

    write = by_name["write_file"]
    assert write["description"].startswith("Write text to a file of the vault")
    assert write["parameters"]["required"] == ["file_name", "content"]
    assert write["parameters"]["properties"]["write_mode"] == {
        "type": "string", "enum": ["a", "w"], "description": '"a" appends to the end (default); "w" replaces the whole file.'}
    assert by_name["replace_in_file"]["parameters"]["properties"]["replace_all"]["type"] == "boolean"
    for fn in by_name.values():
        for prop in fn["parameters"]["properties"].values():
            assert prop["description"] and "default" not in prop  # defaults live in the description


def test_an_undocumented_parameter_is_an_error():
    def tool(path: str) -> str:
        """Do something.

        Args:
            other: Not the parameter.
        """

    with pytest.raises(ValueError, match="'path' is not documented"):
        schema(tool)
