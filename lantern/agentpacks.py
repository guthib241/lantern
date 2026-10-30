"""Agent packs: shareable agents as data-only JSON files, never code.

A pack (`<name>.lantern-agent.json`, at most 32 KB) holds a title, a goal with
`{blanks}` the user fills in, instructions, and the tools the agent may use.
Packs live in Lantern's own folder (`~/.lantern/agents/`, like widgets).

Safety, in code rather than prompts:
  - validation refuses unknown keys, bad names, a `model` field (a pack can
    never pick a model), unknown tools, and any text the secret scrubber flags;
  - installing needs an approval bound to the SHA-256 of the file's bytes;
  - running sets `toolbox.tainted = True` (a shared pack was written by someone
    else) and enforces the tool allowlist in `Toolbox.call`.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import models
from .scrubber import DEFAULT as SCRUBBER

FORMAT = "lantern-agent/1"
MAX_BYTES = 32 * 1024
MAX_TEXT = 200          # title, description, author
MAX_GOAL = 1000
MAX_INSTRUCTIONS = 4000
MAX_INPUTS = 6
MAX_VALUE = 500         # run-time values filled into the goal
STORE_FORMAT = "lantern-agent-store/1"

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
INPUT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
MCP_TOOL = re.compile(r"^[a-z0-9_-]{1,24}\.[A-Za-z0-9_*-]+$")

KEYS = {"format", "name", "title", "description", "author", "version",
        "inputs", "goal", "instructions", "tools"}
INPUT_KEYS = {"key", "question", "example"}


class PackError(ValueError):
    """A pack file is invalid, or a pack operation was refused."""


@dataclass
class Input:
    key: str
    question: str
    example: str = ""

    def as_dict(self) -> dict:
        d = {"key": self.key, "question": self.question}
        if self.example:
            d["example"] = self.example
        return d


@dataclass
class Pack:
    name: str
    title: str
    description: str
    author: str
    version: str
    inputs: list[Input] = field(default_factory=list)
    goal: str = ""
    instructions: str = ""
    tools: list[str] = field(default_factory=list)
    disabled: bool = False

    def render_goal(self, inputs: dict | None) -> str:
        """Fill the `{blanks}` in the goal. Values are plain text: at most
        500 characters, with `{` and `}` removed so they can't add blanks."""
        given = dict(inputs or {})
        out = self.goal
        for key in PLACEHOLDER.findall(self.goal):
            if key not in given:
                raise PackError(f"agent '{self.name}' needs a value for '{key}'")
        for key in given:
            if key not in {i.key for i in self.inputs}:
                raise PackError(f"agent '{self.name}' has no input called '{key}'")
        for key, value in given.items():
            text = str(value)
            if len(text) > MAX_VALUE:
                raise PackError(f"value for '{key}' is too long (at most {MAX_VALUE} characters)")
            out = out.replace("{" + key + "}", text.replace("{", "").replace("}", ""))
        return out

    def as_dict(self) -> dict:
        return {"format": FORMAT, "name": self.name, "title": self.title,
                "description": self.description, "author": self.author, "version": self.version,
                "inputs": [i.as_dict() for i in self.inputs],
                "goal": self.goal, "instructions": self.instructions, "tools": list(self.tools)}

    def heading(self) -> str:
        """The heading under which the pack's instructions run. The agent's own
        instructions always come first, byte-identical, so the cache stays warm."""
        return (f'# Agent pack "{self.title}" by {self.author} '
                f"(installed by the user; Lantern's rules above always win)")


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PackError(f"{what} must be non-empty text")
    if len(value) > limit:
        raise PackError(f"{what} is too long (at most {limit} characters)")
    return value


def validate(data: dict) -> Pack:
    """Check a parsed pack file and return it. Raises PackError with the reason."""
    if not isinstance(data, dict):
        raise PackError("an agent pack must be a JSON object")
    if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_BYTES:
        raise PackError(f"an agent pack is at most {MAX_BYTES // 1024} KB")
    if "model" in data:
        raise PackError("a pack can't pick a model (no 'model' field allowed)")
    if unknown := sorted(set(data) - KEYS):
        raise PackError(f"unknown field{'s' if len(unknown) != 1 else ''}: {', '.join(unknown)}")
    if data.get("format") != FORMAT:
        raise PackError(f"format must be {FORMAT!r}")
    name = data.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise PackError("name must match ^[a-z0-9][a-z0-9-]{0,40}$")
    title = _text(data.get("title"), "title", MAX_TEXT)
    description = _text(data.get("description"), "description", MAX_TEXT)
    author = _text(data.get("author"), "author", MAX_TEXT)
    version = data.get("version")
    if not isinstance(version, str) or not VERSION.match(version):
        raise PackError("version must be plain text like '1.0.0' (at most 32 characters)")
    raw_inputs = data.get("inputs", [])
    if not isinstance(raw_inputs, list) or len(raw_inputs) > MAX_INPUTS:
        raise PackError(f"inputs must be a list of at most {MAX_INPUTS}")
    seen: set[str] = set()
    inputs: list[Input] = []
    for entry in raw_inputs:
        if not isinstance(entry, dict) or set(entry) - INPUT_KEYS or "key" not in entry or "question" not in entry:
            raise PackError("each input needs exactly 'key' and 'question' (plus an optional 'example')")
        key = entry["key"]
        if not isinstance(key, str) or not INPUT_KEY.match(key):
            raise PackError(f"bad input key {key!r}")
        if key in seen:
            raise PackError(f"input '{key}' is listed twice")
        seen.add(key)
        question = _text(entry["question"], f"question for '{key}'", MAX_VALUE)
        example = entry.get("example", "")
        if not isinstance(example, str) or len(example) > MAX_VALUE:
            raise PackError(f"example for '{key}' is too long (at most {MAX_VALUE} characters)")
        inputs.append(Input(key, question, example))
    goal = _text(data.get("goal"), "goal", MAX_GOAL)
    for key in PLACEHOLDER.findall(goal):
        if key not in seen:
            raise PackError(f"goal uses '{{{key}}}' but the pack has no such input")
    instructions = _text(data.get("instructions"), "instructions", MAX_INSTRUCTIONS)
    raw_tools = data.get("tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        raise PackError("tools must be a non-empty list")
    from .tools import TOOL_NAMES
    tools: list[str] = []
    for tool in raw_tools:
        if not isinstance(tool, str) or not tool:
            raise PackError("each tool must be a name like 'search_files' or 'freecad.*'")
        if tool == "finish" or tool in TOOL_NAMES or MCP_TOOL.match(tool):
            tools.append(tool)
        else:
            raise PackError(f"unknown tool {tool!r}")
    pack = Pack(name, title, description, author, version, inputs, goal, instructions, tools)
    _refuse_secrets(pack)
    return pack


def _refuse_secrets(pack: Pack) -> None:
    """Run the secret scrubber over every text field; refuse the pack if found."""
    texts = [pack.title, pack.description, pack.author, pack.version,
             pack.goal, pack.instructions, *pack.tools]
    texts += [t for i in pack.inputs for t in (i.key, i.question, i.example)]
    for text in texts:
        if SCRUBBER.scrub(text).count:
            raise PackError("that pack holds something that looks like a secret; refusing it")


def canonical_bytes(data: dict) -> bytes:
    """The file's bytes: deterministic, so exports round-trip and the install
    approval can bind to their SHA-256."""
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


@dataclass
class PackStore:
    """Packs in Lantern's own folder. Files hold the pack plus a `disabled`
    flag; there are no deletes. Files are re-validated on every load."""

    dir: str | Path

    def __init__(self, dir: str | Path):
        self.dir = Path(dir).expanduser()

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def list(self) -> list[dict]:
        out = []
        if self.dir.exists():
            for path in sorted(self.dir.glob("*.json")):
                try:
                    pack, disabled = self._load(path)
                except (PackError, ValueError, OSError):
                    continue   # a hand-edited file that no longer validates stays unlisted
                out.append({**pack.as_dict(), "disabled": disabled})
        return out

    def get(self, name: str) -> Pack:
        """The pack, with its disabled flag. Raises KeyError if not installed."""
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"no agent called {name!r}")
        pack, disabled = self._load(path)
        pack.disabled = disabled
        return pack

    def _load(self, path: Path) -> tuple[Pack, bool]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("pack"), dict):
            raise PackError(f"{path.name} is not an agent pack file")
        return validate(raw["pack"]), bool(raw.get("disabled", False))

    def install(self, data: dict, *, replace: bool = False) -> Pack:
        """Validate and store `<name>.json`. Refuses to overwrite a different
        author's pack with the same name unless `replace=True`."""
        pack = validate(data)
        path = self._path(pack.name)
        if path.exists():
            try:
                old, _ = self._load(path)
            except (PackError, ValueError, OSError) as e:
                raise PackError(f"existing file for {pack.name!r} is broken ({e}); refusing to touch it") from e
            if old.author != pack.author and not replace:
                raise PackError(f"an agent called {pack.name!r} by {old.author} is already installed "
                                f"(pass replace=True to take its place)")
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": STORE_FORMAT, "disabled": False, "pack": pack.as_dict()},
                                   ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        stored = self.get(pack.name)   # verify: read it back
        if stored.as_dict() != pack.as_dict():
            raise PackError(f"agent {pack.name!r} didn't store correctly")
        return stored

    def disable(self, name: str) -> Pack:
        return self._set_disabled(name, True)

    def enable(self, name: str) -> Pack:
        return self._set_disabled(name, False)

    def _set_disabled(self, name: str, disabled: bool) -> Pack:
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"no agent called {name!r}")
        pack, _ = self._load(path)
        path.write_text(json.dumps({"format": STORE_FORMAT, "disabled": disabled, "pack": pack.as_dict()},
                                   ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pack.disabled = disabled
        return pack

    def export(self, name: str) -> bytes:
        """The exact pack file, to share."""
        return canonical_bytes(self.get(name).as_dict())


def tool_allowed(allowed: set[str] | None, name: str) -> bool:
    """The pack's allowlist, enforced in `Toolbox.call`. `freecad.*` matches
    MCP tool names like `freecad.box`; `finish` is always allowed."""
    if allowed is None or name == "finish" or name in allowed:
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in allowed)


def pack_suffix(pack: Pack) -> str:
    """The pack's instructions, appended after "Now:" at the end of the
    agent's instructions (the prefix stays byte-stable for prompt caching)."""
    return f"{pack.heading()}\n{pack.instructions}"


# ---- drafting a pack from a finished task --------------------------------------
DRAFT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "title", "description", "author", "version", "inputs", "goal", "instructions", "tools"],
    "properties": {
        "name": {"type": "string", "description": "short id like 'tidy-downloads'"},
        "title": {"type": "string", "description": "display name"},
        "description": {"type": "string", "description": "one line on what it does"},
        "author": {"type": "string", "description": "who drafts it, or '' when unknown"},
        "version": {"type": "string", "description": "'1.0.0' for a first draft"},
        "inputs": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["key", "question", "example"],
            "properties": {"key": {"type": "string"}, "question": {"type": "string"},
                           "example": {"type": "string"}}}},
        "goal": {"type": "string", "description": "the goal with concrete values turned into {blanks}"},
        "instructions": {"type": "string", "description": "how to do it, naming the tools to use"},
        "tools": {"type": "array", "items": {"type": "string"}},
    },
}
DRAFT_RULES = ("You turn a finished task into a reusable agent pack: the same job, with its concrete "
               "values turned into {blanks} the next user fills in. Every {blank} in the goal must have "
               "a matching input with a short question and an example. List only tools the task actually "
               "used (plus finish, which is always allowed). Never invent a name, author or version: use '' "
               "for an unknown author and '1.0.0' for the version. Instructions are plain steps, never code, "
               "and must not contain secrets. The format field is always 'lantern-agent/1'.")


def draft_pack(client: Any, memory, task_id: int) -> tuple[dict, Any]:
    """Ask Muse Spark (structured output, same shape as the FreeCAD connector's
    expectations) to draft a pack from a task's goal and the tools it used."""
    task = memory.get_task(task_id)   # KeyError if unknown
    used: list[str] = []
    for step in memory.steps(task_id):
        if step["kind"] != "tool_call":
            continue
        try:
            name = json.loads(step["content"]).get("name")
        except (TypeError, ValueError):
            continue
        if name and name != "finish" and name not in used:
            used.append(name)
    brief = {"goal": task.goal, "summary": task.summary or "", "tools_used": used}
    response = client.responses.create(
        model=models.check_model(models.SPARK), instructions=DRAFT_RULES,
        reasoning={"effort": "low"},
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": "Draft an agent pack for this finished task:\n"
             + json.dumps(brief, ensure_ascii=False)}]}],
        text={"format": {"type": "json_schema", "name": "agent_pack_draft",
                         "schema": DRAFT_SCHEMA, "strict": True}})
    from .models import Usage, token_cost
    usage = Usage.from_response(getattr(response, "usage", None))
    memory.log_usage(task_id, models.SPARK, usage.input_tokens, usage.cached_tokens,
                     usage.output_tokens, token_cost(models.SPARK, usage))
    try:
        data = json.loads(getattr(response, "output_text", "") or "")
    except json.JSONDecodeError as e:
        raise PackError(f"the draft wasn't valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise PackError("the draft wasn't an agent pack")
    data.setdefault("format", FORMAT)
    return validate(data).as_dict(), response



