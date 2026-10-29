"""The file tools over the vault, their schemas, and one dispatcher that never raises.

Two rules are enforced here, in the tools themselves, so no hook, approval or `--yes` can get around them:
- **sandbox**: every path must resolve inside the vault (no absolute paths, `~`, `..` or symlinks that lead out);
- **protected paths**: the agent's own code and state can't be written, and `.env` files can't be touched at all.

Each tool's schema is generated from its signature and Google-style docstring: one registry, nothing to drift.
"""

from __future__ import annotations

import inspect
import json
import typing
from pathlib import Path
from typing import Callable, Iterable, Literal

from sa.config import PROJECT, Config

SELF = (PROJECT / "main.py", PROJECT / "sa", PROJECT / "spec")  # the agent's own code: never written by the agent
SECRET_NAMES = {".env"}  # never read or written, wherever they are
PATH_ARGS = ("file_name", "directory", "directory_path")


def error(message: str) -> str:
    """A tool error the model can read and recover from."""
    return json.dumps({"error": message}, ensure_ascii=False)


def ask_in_terminal(question: str) -> str:
    print(f"\n❓ {question}\n")
    try:
        return input("🧑 Your answer: ")
    except (EOFError, OSError):
        return "(no answer: input is closed)"


class Tools:
    """The agent's tools, bound to one vault.

    `ask` answers `user_input` (the terminal by default; evals script it). `protect` lists paths that may not be
    written even inside the vault (see `make_tools`).
    """

    def __init__(self, root: Path, ask: Callable[[str], str] = ask_in_terminal, protect: Iterable[Path] = ()):
        self.root = Path(root).resolve()
        self.ask = ask
        self.protect = tuple(Path(p).resolve() for p in protect)
        self.registry: dict[str, Callable[..., str]] = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "replace_in_file": self.replace_in_file,
            "remove_from_file": self.remove_from_file,
            "list_dir": self.list_dir,
            "make_dir": self.make_dir,
            "user_input": self.user_input,
        }
        self._schemas = {name: schema(fn) for name, fn in self.registry.items()}

    def schemas(self) -> list[dict]:
        """What the model is told it can call: exactly the registry, nothing it can't run."""
        return list(self._schemas.values())

    def call(self, name: str, args: dict) -> str:
        """Run one tool. Unknown names, wrong arguments and failures come back as {"error": ...}, never as a crash."""
        fn = self.registry.get(name)
        if fn is None:
            return error(f"Unknown tool {name!r}. Available tools: {', '.join(self.registry)}.")
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as e:
            expected = list(self._schemas[name]["function"]["parameters"]["properties"])
            return error(f"Bad arguments for {name}: {e}. Expected: {expected}")
        try:
            return fn(**args)
        except Exception as e:  # noqa: BLE001 - the model gets the error and can try something else
            return error(f"{name} failed: {type(e).__name__}: {e}")

    def refusal(self, name: str, args: dict) -> str | None:
        """Why the sandbox would refuse this call (checked before asking a human), or None."""
        write = name in WRITE_TOOLS
        for key in PATH_ARGS:
            if isinstance(args.get(key), str):
                try:
                    self._safe(args[key], write=write)
                except PermissionError as e:
                    return str(e)
        return None

    def _safe(self, name: str, write: bool = False) -> Path:
        """The real path for a vault path, or PermissionError if it leads outside the vault or to a protected path."""
        raw = Path(name)
        if raw.is_absolute() or name.startswith("~"):
            raise PermissionError(f"{name!r} is not a vault path: use a path relative to the vault, "
                                  f"like 'projects/notes.md'.")
        path = (self.root / raw).resolve()  # follows symlinks and '..'
        if not path.is_relative_to(self.root):
            raise PermissionError(f"{name!r} leads outside the vault. Only files inside the vault can be used.")
        if path.name in SECRET_NAMES:
            raise PermissionError(f"{name!r} is a secrets file: it can't be read or written.")
        if write and any(path == p or path.is_relative_to(p) for p in self.protect):
            raise PermissionError(f"{name!r} is part of the assistant's own code or state: it can't be changed "
                                  f"from here. Explain the change to Sami instead.")
        return path

    def _shown(self, path: Path) -> str:
        """A path as the model should see it: relative to the vault."""
        return path.relative_to(self.root).as_posix() or "."

    # ---------------------------------------------------------------- the tools
    def read_file(self, file_name: str) -> str:
        """Read a text file of the vault.

        Args:
            file_name: Path of the file, relative to the vault (e.g. "MEMORY.MD", "projects/gp/plan.md").
        """
        path = self._safe(file_name)
        return f"Reading file {self._shown(path)} content: {path.read_text(encoding='utf-8')}"

    def write_file(self, file_name: str, content: str, write_mode: Literal["a", "w"] = "a") -> str:
        """Write text to a file of the vault: append to it, or overwrite it. The folder must exist (use make_dir).

        Args:
            file_name: Path of the file, relative to the vault.
            content: The text to write.
            write_mode: "a" appends to the end (default); "w" replaces the whole file.
        """
        if write_mode not in ("a", "w"):
            raise ValueError("write_mode must be 'a' (append) or 'w' (overwrite)")
        path = self._safe(file_name, write=True)
        with open(path, write_mode, encoding="utf-8") as f:
            f.write(content)
        return f"Done writing to file: {self._shown(path)} ({len(content)} characters, mode {write_mode!r})"

    def replace_in_file(self, file_name: str, old_content: str, new_content: str, replace_all: bool = False) -> str:
        """Replace exact text in a file of the vault. Read the file first and copy the text exactly.

        Args:
            file_name: Path of the file, relative to the vault.
            old_content: The exact text to find. It must appear exactly once, unless replace_all is true.
            new_content: The text to put in its place.
            replace_all: Replace every occurrence instead of exactly one. Default: false.
        """
        count = self._edit(file_name, old_content, new_content, replace_all)
        return f"Replaced {count} occurrence{'s' if count > 1 else ''} in {file_name}: " \
               f"{_short(old_content)!r} → {_short(new_content)!r}"

    def remove_from_file(self, file_name: str, content_to_remove: str, replace_all: bool = False) -> str:
        """Remove exact text from a file of the vault. Read the file first and copy the text exactly.

        Args:
            file_name: Path of the file, relative to the vault.
            content_to_remove: The exact text to remove. It must appear exactly once, unless replace_all is true.
            replace_all: Remove every occurrence instead of exactly one. Default: false.
        """
        count = self._edit(file_name, content_to_remove, "", replace_all)
        return f"Removed {count} occurrence{'s' if count > 1 else ''} of {_short(content_to_remove)!r} from {file_name}"

    def list_dir(self, directory: str = ".") -> str:
        """List a folder of the vault. Folder names end with '/'.

        Args:
            directory: Folder path relative to the vault. Default: "." (the vault itself).
        """
        path = self._safe(directory)
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir() if p.name not in SECRET_NAMES)
        return json.dumps(names, ensure_ascii=False)

    def make_dir(self, directory_path: str) -> str:
        """Create a folder in the vault, with any missing parents. Does nothing if it already exists.

        Args:
            directory_path: Folder path relative to the vault (e.g. "projects/graduation").
        """
        path = self._safe(directory_path, write=True)
        path.mkdir(parents=True, exist_ok=True)
        return f"Directory created/verified: {self._shown(path)}"

    def user_input(self, question: str) -> str:
        """Ask Sami a question and wait for the answer. Only for decisions you can't make yourself.

        Args:
            question: The question to ask, in Sami's language.
        """
        return self.ask(question)

    def _edit(self, file_name: str, old: str, new: str, replace_all: bool) -> int:
        """Replace `old` with `new` in a file; exactly one match unless `replace_all`. Returns the match count."""
        if not old:
            raise ValueError("the text to find is empty. Read the file and copy the exact text to change.")
        path = self._safe(file_name, write=True)
        content = path.read_text(encoding="utf-8")
        count = content.count(old)
        if count == 0:
            raise ValueError(f"{_short(old)!r} was not found in {file_name}. Read the file and copy the exact text.")
        if count > 1 and not replace_all:
            raise ValueError(f"{_short(old)!r} appears {count} times in {file_name}. Include more of the surrounding "
                             f"text so it appears once, or set replace_all to change every occurrence.")
        path.write_text(content.replace(old, new) if replace_all else content.replace(old, new, 1), encoding="utf-8")
        return count


WRITE_TOOLS = {"write_file", "replace_in_file", "remove_from_file", "make_dir"}


def make_tools(cfg: Config, ask: Callable[[str], str] = ask_in_terminal, allow_self_edit: bool = False) -> Tools:
    """The tools as the CLI uses them: the agent's own code and its state folder (sessions, audit log, traces)
    protected, unless Sami started it with --allow-self-edit."""
    return Tools(cfg.root, ask=ask, protect=() if allow_self_edit else (*SELF, cfg.state))


def _short(text: str, width: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width] + "…"


# ---------------------------------------------------------------- schemas from functions
JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def schema(fn: Callable) -> dict:
    """A Chat Completions tool definition from a function's type hints and Google-style docstring.

    Every parameter must be documented under "Args:", so a tool can't reach the model undocumented. Defaults are
    stated in the description, not as a JSON Schema "default": not every provider accepts that keyword.
    """
    doc = inspect.getdoc(fn) or ""
    summary, _, args_doc = doc.partition("\nArgs:\n")
    docs: dict[str, str] = {}
    current = None
    for line in args_doc.splitlines():
        if line.startswith("    ") and not line.startswith("        ") and ":" in line:
            current, text = line.strip().split(":", 1)
            docs[current] = text.strip()
        elif current and line.strip():
            docs[current] += " " + line.strip()

    hints = typing.get_type_hints(fn)
    properties, required = {}, []
    for name, param in inspect.signature(fn).parameters.items():
        if name not in docs:
            raise ValueError(f"{fn.__name__}: parameter {name!r} is not documented under Args:")
        hint = hints.get(name, str)
        if typing.get_origin(hint) is Literal:
            prop = {"type": "string", "enum": list(typing.get_args(hint))}
        elif hint in JSON_TYPES:
            prop = {"type": JSON_TYPES[hint]}
        else:
            raise TypeError(f"{fn.__name__}: unsupported type {hint!r} for {name!r}")
        prop["description"] = docs[name]
        properties[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "function", "function": {"name": fn.__name__, "description": " ".join(summary.split()),
                                             "parameters": {"type": "object", "properties": properties,
                                                            "required": required}}}
