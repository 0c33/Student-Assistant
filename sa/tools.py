"""The file tools over the vault, their schemas, and one dispatcher that never raises.

Relative paths resolve against the vault (SA_ROOT). Phase 3 adds the sandbox (`_safe`), unique-match edits and
schemas generated from the functions; until then the schemas are written by hand next to the registry.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Callable


def error(message: str) -> str:
    """A tool error the model can read and recover from."""
    return json.dumps({"error": message}, ensure_ascii=False)


def ask_in_terminal(question: str) -> str:
    print(f"\n❓ {question}\n")
    try:
        return input("🧑 Your answer: ")
    except EOFError:
        return "(no answer: input is closed)"


class Tools:
    """The agent's tools, bound to one vault. `ask` answers `user_input` (the terminal by default; evals script it)."""

    def __init__(self, root: Path, ask: Callable[[str], str] = ask_in_terminal):
        self.root = Path(root)
        self.ask = ask
        self.registry: dict[str, Callable[..., str]] = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "replace_in_file": self.replace_in_file,
            "remove_from_file": self.remove_from_file,
            "list_dir": self.list_dir,
            "make_dir": self.make_dir,
            "user_input": self.user_input,
        }

    def schemas(self) -> list[dict]:
        """What the model is told it can call: exactly the registry, nothing it can't run."""
        return [{"type": "function", "function": {"name": name, **SCHEMAS[name]}} for name in self.registry]

    def call(self, name: str, args: dict) -> str:
        """Run one tool. Unknown names, wrong arguments and failures come back as {"error": ...}, never as a crash."""
        fn = self.registry.get(name)
        if fn is None:
            return error(f"Unknown tool {name!r}. Available tools: {', '.join(self.registry)}.")
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as e:
            return error(f"Bad arguments for {name}: {e}. Expected: {SCHEMAS[name]['parameters']['properties']}")
        try:
            return fn(**args)
        except Exception as e:  # noqa: BLE001 - the model gets the error and can try something else
            return error(f"{name} failed: {type(e).__name__}: {e}")

    def _path(self, name: str) -> Path:
        path = Path(name).expanduser()
        return path if path.is_absolute() else self.root / path

    # ---------------------------------------------------------------- the tools
    def read_file(self, file_name: str) -> str:
        return f"Reading file {file_name} content: {self._path(file_name).read_text(encoding='utf-8')}"

    def write_file(self, file_name: str, content: str, write_mode: str = "a") -> str:
        if write_mode not in ("a", "w"):
            raise ValueError("write_mode must be 'a' (append) or 'w' (overwrite)")
        with open(self._path(file_name), write_mode, encoding="utf-8") as f:
            f.write(content)
        return f"Done writing to file: {file_name} ({len(content)} characters, mode {write_mode!r})"

    def replace_in_file(self, file_name: str, old_content: str, new_content: str) -> str:
        path = self._path(file_name)
        content = path.read_text(encoding="utf-8")
        if old_content not in content:
            return error(f'"{old_content}" not found in {file_name}. Read the file and copy the exact text.')
        path.write_text(content.replace(old_content, new_content), encoding="utf-8")
        return f'Successfully replaced "{old_content}" with "{new_content}" in {file_name}'

    def remove_from_file(self, file_name: str, content_to_remove: str) -> str:
        path = self._path(file_name)
        content = path.read_text(encoding="utf-8")
        if content_to_remove not in content:
            return error(f'"{content_to_remove}" not found in {file_name}. Read the file and copy the exact text.')
        path.write_text(content.replace(content_to_remove, ""), encoding="utf-8")
        return f'Successfully removed "{content_to_remove}" from {file_name}'

    def list_dir(self, directory: str = ".") -> str:
        path = self._path(directory)
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return json.dumps(names, ensure_ascii=False)

    def make_dir(self, directory_path: str) -> str:
        self._path(directory_path).mkdir(parents=True, exist_ok=True)
        return f"Directory created/verified: {directory_path}"

    def user_input(self, question: str) -> str:
        return self.ask(question)


SCHEMAS = {
    "read_file": {
        "description": "Read the contents of a file in the vault.",
        "parameters": {"type": "object", "properties": {
            "file_name": {"type": "string", "description": "Path of the file, relative to the vault"},
        }, "required": ["file_name"]},
    },
    "write_file": {
        "description": "Write or append content to a file in the vault (the folder must exist: use make_dir first).",
        "parameters": {"type": "object", "properties": {
            "file_name": {"type": "string", "description": "Path of the file, relative to the vault"},
            "content": {"type": "string", "description": "The content to write"},
            "write_mode": {"type": "string", "enum": ["a", "w"], "description": "Append (a, default) or overwrite (w)"},
        }, "required": ["file_name", "content"]},
    },
    "replace_in_file": {
        "description": "Replace exact text inside a file. Read the file first and copy the text exactly.",
        "parameters": {"type": "object", "properties": {
            "file_name": {"type": "string", "description": "Path of the file, relative to the vault"},
            "old_content": {"type": "string", "description": "Exact text to find"},
            "new_content": {"type": "string", "description": "Text to replace it with"},
        }, "required": ["file_name", "old_content", "new_content"]},
    },
    "remove_from_file": {
        "description": "Remove exact text from a file. Read the file first and copy the text exactly.",
        "parameters": {"type": "object", "properties": {
            "file_name": {"type": "string", "description": "Path of the file, relative to the vault"},
            "content_to_remove": {"type": "string", "description": "Exact text to remove"},
        }, "required": ["file_name", "content_to_remove"]},
    },
    "list_dir": {
        "description": "List a folder of the vault. Folders end with '/'.",
        "parameters": {"type": "object", "properties": {
            "directory": {"type": "string", "description": "Folder path relative to the vault ('.' = the vault)"},
        }, "required": []},
    },
    "make_dir": {
        "description": "Create a folder in the vault (and its parents). Does nothing if it exists.",
        "parameters": {"type": "object", "properties": {
            "directory_path": {"type": "string", "description": "Folder path relative to the vault"},
        }, "required": ["directory_path"]},
    },
    "user_input": {
        "description": "Ask Sami a question and wait for his answer. Only for decisions you can't make yourself.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "The question to ask"},
        }, "required": ["question"]},
    },
}
