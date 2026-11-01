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

import datetime as dt
import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import models, packsign
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
        "inputs", "goal", "instructions", "tools", "schedule", "signature"}
INPUT_KEYS = {"key", "question", "example"}
SCHEDULE_KEYS = {"every", "at", "hours", "inputs"}
SCHEDULE_EVERY = ("day", "weekday", "week", "hours")
AT = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MIN_INTERVAL_HOURS = 1.0   # the shortest loop: every hour
MAX_LOOP_FAILURES = 3      # failed/stopped runs in a row before the loop pauses itself
EMOJI_MAX = 8             # an emoji face is a glance, not a sentence
FACE_ICON_SUFFIX = ".webp"   # the generated icon lives next to the pack as <name>.webp


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
class Schedule:
    """A loop suggestion from the pack file: when to run on its own. It starts
    OFF when installed; the user turns it on per pack (Core's loop state)."""
    every: str = "day"          # day | weekday | week | hours
    at: str | None = None       # "09:00" local time (day, weekday, week)
    hours: float | None = None  # every N hours, at least 1 (hours)
    inputs: dict = field(default_factory=dict)  # blanks for the goal at each run

    def as_dict(self) -> dict:
        d: dict = {"every": self.every}
        if self.at is not None:
            d["at"] = self.at
        if self.hours is not None:
            d["hours"] = self.hours
        if self.inputs:
            d["inputs"] = dict(self.inputs)
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
    schedule: Schedule | None = None   # a loop suggestion; None means no schedule
    signature: dict | None = None   # Ed25519 trust seal (packsign.py); None when unsigned

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
        d: dict = {"format": FORMAT, "name": self.name, "title": self.title,
                   "description": self.description, "author": self.author, "version": self.version,
                   "inputs": [i.as_dict() for i in self.inputs],
                   "goal": self.goal, "instructions": self.instructions, "tools": list(self.tools)}
        if self.schedule is not None:
            d["schedule"] = self.schedule.as_dict()
        if self.signature is not None:
            d["signature"] = dict(self.signature)
        return d

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
    signature = _checked_signature(data.get("signature"))
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
    schedule = _validate_schedule(data.get("schedule"), seen, goal)
    pack = Pack(name, title, description, author, version, inputs, goal, instructions, tools,
                schedule=schedule, signature=signature)
    _refuse_secrets(pack)
    if signature is not None and packsign.verify(data) == packsign.TAMPERED:
        raise PackError("this pack was changed after it was signed")
    return pack


def _validate_schedule(raw: Any, input_keys: set[str], goal: str) -> Schedule | None:
    """The optional loop suggestion. Strict: unknown keys, a bad `every`, a clock
    time that isn't HH:MM, an interval under an hour, or inputs the pack doesn't
    declare are all refused. Every `{blank}` in the goal needs a schedule input,
    or the loop could never run on its own."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PackError("a pack's schedule must be an object")
    if unknown := sorted(set(raw) - SCHEDULE_KEYS):
        raise PackError(f"unknown schedule field{'s' if len(unknown) != 1 else ''}: {', '.join(unknown)}")
    every = raw.get("every")
    if every not in SCHEDULE_EVERY:
        raise PackError(f"schedule.every must be one of {', '.join(SCHEDULE_EVERY)}")
    at = raw.get("at")
    hours = raw.get("hours")
    if every == "hours":
        if at is not None:
            raise PackError("a schedule with every='hours' takes 'hours', not 'at'")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)):
            raise PackError("a schedule with every='hours' needs a number in 'hours'")
        if hours < MIN_INTERVAL_HOURS:
            raise PackError("a schedule with every='hours' needs at least 1 hour")
        hours = float(hours)
    else:
        if hours is not None:
            raise PackError(f"a schedule with every={every!r} takes 'at' (HH:MM), not 'hours'")
        if not isinstance(at, str) or not AT.match(at):
            raise PackError(f"a schedule with every={every!r} needs 'at' as HH:MM (e.g. '09:00')")
    sched_inputs = raw.get("inputs", {})
    if not isinstance(sched_inputs, dict):
        raise PackError("a schedule's inputs must be an object")
    for key, value in sched_inputs.items():
        if key not in input_keys:
            raise PackError(f"the schedule fills '{key}' but the pack has no such input")
        if not isinstance(value, str) or len(value) > MAX_VALUE:
            raise PackError(f"schedule value for '{key}' must be plain text (at most {MAX_VALUE} characters)")
    for key in PLACEHOLDER.findall(goal):
        if key not in sched_inputs:
            raise PackError(f"the schedule must fill '{{{key}}}' or the loop could never run")
    return Schedule(every=every, at=at, hours=hours, inputs=dict(sched_inputs))


def _at_on(day: dt.datetime, at: str) -> dt.datetime:
    """That date at HH:MM local time (naive local, like reminders.py)."""
    hour, minute = int(at[:2]), int(at[3:])
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def schedule_next(schedule: Schedule, after: float, last: float | None = None) -> float:
    """The next run strictly after `after` (a timestamp; `last` is the previous
    run, for the weekly anchor and the hourly cadence). Pure: tests pass a fake clock."""
    if schedule.every == "hours":
        interval = float(schedule.hours or MIN_INTERVAL_HOURS)
        base = last if last is not None else after
        nxt = base + interval * 3600
        while nxt <= after:
            nxt += interval * 3600
        return nxt
    at = schedule.at or "09:00"
    if schedule.every == "week" and last is not None:
        cand = _at_on(dt.datetime.fromtimestamp(last) + dt.timedelta(days=7), at)
        while cand.timestamp() <= after:
            cand += dt.timedelta(days=7)
        return cand.timestamp()
    base = dt.datetime.fromtimestamp(after)
    for ahead in range(8):   # a weekday run is never more than a weekend away
        cand = _at_on(base + dt.timedelta(days=ahead), at)
        if cand.timestamp() > after and (schedule.every != "weekday" or cand.weekday() < 5):
            return cand.timestamp()
    raise PackError("no weekday in the next 8 days")  # unreachable


def schedule_description(schedule: Schedule) -> str:
    """Plain words for the approval card and the Agents tab: 'every day at 09:00'."""
    if schedule.every == "hours":
        n = float(schedule.hours or MIN_INTERVAL_HOURS)
        word = "hour" if n == 1 else "hours"
        return f"every {n:g} {word}"
    at = schedule.at or "09:00"
    return {"day": f"every day at {at}", "weekday": f"every weekday at {at}",
            "week": f"every week at {at}"}[schedule.every]


def _checked_signature(sig: Any) -> dict | None:
    """The optional `signature` field: strict shape, then verified. A pack whose
    content no longer matches its signature is refused. Raises PackError."""
    if sig is None:
        return None
    try:
        packsign.check_shape(sig)
    except packsign.PackSignError as e:
        raise PackError(str(e)) from None
    return dict(sig)


def _refuse_secrets(pack: Pack) -> None:
    """Run the secret scrubber over every text field; refuse the pack if found."""
    texts = [pack.title, pack.description, pack.author, pack.version,
             pack.goal, pack.instructions, *pack.tools]
    texts += [t for i in pack.inputs for t in (i.key, i.question, i.example)]
    if pack.schedule is not None:
        texts += [str(v) for v in pack.schedule.inputs.values()]
    for text in texts:
        if SCRUBBER.scrub(text).count:
            raise PackError("that pack holds something that looks like a secret; refusing it")


def validate_emoji(value: Any) -> str:
    """An emoji face: short plain text, secrets scrubbed. Raises PackError."""
    if not isinstance(value, str) or not value.strip():
        raise PackError("a face emoji can't be empty; type one or ask Lantern to draw an icon")
    text = value.strip()
    if len(text) > EMOJI_MAX:
        raise PackError(f"a face emoji is at most {EMOJI_MAX} characters")
    if SCRUBBER.scrub(text).count:
        raise PackError("that face holds something that looks like a secret; refusing it")
    return text


def face_prompt(pack: Pack) -> str:
    """One small icon for this agent, in words. No logos, no text, no brand marks."""
    return (f"Small round app icon for a helpful AI agent called '{pack.title}': {pack.description} "
            "Centred symbol on a plain soft dark background, flat vector style, high contrast. "
            "No logos, no text, no brand marks, no letters, no watermark.")


def canonical_bytes(data: dict) -> bytes:
    """The file's bytes: deterministic, so exports round-trip and the install
    approval can bind to their SHA-256."""
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


SURFACES = ("laptop", "phone", "both")
DEFAULT_SURFACE = "laptop"


def validate_surface(value: Any) -> str:
    """Where an agent can start from (`laptop`, `phone` or `both`). Raises PackError."""
    if value not in SURFACES:
        raise PackError(f"surface must be one of {', '.join(SURFACES)}")
    return value


LOOP_DEFAULT = {"enabled": False, "next_run": None, "last_task": None,
                "last_status": None, "failures": 0, "running": None}
LOOP_TYPES = {"enabled": bool, "next_run": (int, float, type(None)), "last_task": (int, type(None)),
              "last_status": (str, type(None)), "failures": int, "running": (int, type(None))}


def _loop_of(raw: Any, *, strict: bool = False) -> dict:
    """Local loop state from a store wrapper. Missing or hand-edited state means
    OFF; `save_loop` is strict and refuses unknown or wrongly typed fields."""
    if raw is None:
        return dict(LOOP_DEFAULT)
    if not isinstance(raw, dict):
        if strict:
            raise PackError("a loop must be an object")
        return dict(LOOP_DEFAULT)
    if strict and (unknown := sorted(set(raw) - set(LOOP_DEFAULT))):
        raise PackError(f"unknown loop field{'s' if len(unknown) != 1 else ''}: {', '.join(unknown)}")
    loop = dict(LOOP_DEFAULT)
    for key in LOOP_DEFAULT:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) and LOOP_TYPES[key] is int:   # True is an int, but not a count
            if strict:
                raise PackError(f"loop field '{key}' must be a number")
            continue
        if not isinstance(value, LOOP_TYPES[key]):
            if strict:
                raise PackError(f"loop field '{key}' has the wrong type")
            continue
        loop[key] = value
    if not isinstance(loop["failures"], int) or isinstance(loop["failures"], bool) or loop["failures"] < 0:
        if strict:
            raise PackError("loop field 'failures' must be a number")
        loop["failures"] = 0
    return loop


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
                    pack, disabled, loop = self._load(path)
                except (PackError, ValueError, OSError):
                    continue   # a hand-edited file that no longer validates stays unlisted
                info = packsign.signature_info(pack.as_dict())
                out.append({**pack.as_dict(), "disabled": disabled, "loop": loop,
                            "surface": self._stored_surface(path) or DEFAULT_SURFACE,
                            "face": self._listed_face(path, pack.name),
                            "signature_status": info["status"], "fingerprint": info["fingerprint"]})
        return out

    def _listed_face(self, path: Path, name: str) -> dict | None:
        """The face for a list entry: the generated icon wins over a stored emoji."""
        if (self.dir / f"{name}{FACE_ICON_SUFFIX}").exists():
            return {"icon": True}
        emoji = self._stored_face(path)
        return {"emoji": emoji} if emoji else None

    def get(self, name: str) -> Pack:
        """The pack, with its disabled flag. Raises KeyError if not installed."""
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"no agent called {name!r}")
        pack, disabled, _ = self._load(path)
        pack.disabled = disabled
        return pack

    def _load(self, path: Path) -> tuple[Pack, bool, dict]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("pack"), dict):
            raise PackError(f"{path.name} is not an agent pack file")
        return validate(raw["pack"]), bool(raw.get("disabled", False)), _loop_of(raw.get("loop"))

    @staticmethod
    def _stored_face(path: Path) -> str | None:
        """The emoji face kept in the store wrapper (never in the shared pack file)."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        face = raw.get("face") if isinstance(raw, dict) else None
        emoji = face.get("emoji") if isinstance(face, dict) else None
        return emoji if isinstance(emoji, str) and emoji else None

    @staticmethod
    def _stored_surface(path: Path) -> str | None:
        """The Runs-on setting in the store wrapper (never in the shared pack file)."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        surface = raw.get("surface") if isinstance(raw, dict) else None
        return surface if surface in SURFACES else None

    def _write(self, pack: Pack, *, disabled: bool, face: dict | None, loop: dict | None = None,
               surface: str | None = None) -> None:
        """Store the wrapper: the pack plus local-only state (disabled, face, loop, surface)."""
        if surface is None:   # keep the existing setting; a fresh install starts on the laptop
            surface = self._stored_surface(self._path(pack.name)) or DEFAULT_SURFACE
        wrapper: dict[str, Any] = {"format": STORE_FORMAT, "disabled": disabled, "pack": pack.as_dict(),
                                   "surface": surface}
        if face:
            wrapper["face"] = face
        wrapper["loop"] = _loop_of(loop)   # the loop is local state, never shared
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(pack.name).write_text(json.dumps(wrapper, ensure_ascii=False, indent=2,
                                                    sort_keys=True) + "\n", encoding="utf-8")

    def install(self, data: dict, *, replace: bool = False) -> Pack:
        """Validate and store `<name>.json`. Refuses to overwrite a different
        author's pack with the same name unless `replace=True`. A fresh install
        starts the loop OFF, even when the pack suggests a schedule."""
        pack = validate(data)
        path = self._path(pack.name)
        face = self._stored_face(path) if path.exists() else None
        if path.exists():
            try:
                old, _, _ = self._load(path)
            except (PackError, ValueError, OSError) as e:
                raise PackError(f"existing file for {pack.name!r} is broken ({e}); refusing to touch it") from e
            if old.author != pack.author and not replace:
                raise PackError(f"an agent called {pack.name!r} by {old.author} is already installed "
                                f"(pass replace=True to take its place)")
        self._write(pack, disabled=False, face={"emoji": face} if face else None, loop=None)
        stored = self.get(pack.name)   # verify: read it back
        if stored.as_dict() != pack.as_dict():
            raise PackError(f"agent {pack.name!r} didn't store correctly")
        return stored

    def store_signed(self, name: str, data: dict) -> Pack:
        """Keep a freshly signed pack: same name, validated, with the laptop's
        disabled flag, face and loop left alone. Raises PackError when refused."""
        pack = validate(data)   # refuses a tampered file, like every other load
        if pack.name != name:
            raise PackError(f"signed pack is for {pack.name!r}, not {name!r}")
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"no agent called {name!r}")
        face = self._stored_face(path)
        _, disabled, loop = self._load(path)
        self._write(pack, disabled=disabled, face={"emoji": face} if face else None, loop=loop)
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
        pack, _, loop = self._load(path)
        face = self._stored_face(path)
        self._write(pack, disabled=disabled, face={"emoji": face} if face else None, loop=loop)
        pack.disabled = disabled
        return pack

    # ---- loops: a pack's schedule suggestion, and whether it runs on its own --
    def loop_state(self, name: str) -> dict:
        """Local loop state for a pack. Defaults to OFF. Raises KeyError if unknown."""
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"no agent called {name!r}")
        _, _, loop = self._load(path)   # re-validates the pack, like every other load
        return loop

    def save_loop(self, name: str, loop: dict) -> dict:
        """Keep new loop state (enabled, next_run, last task, failures). The pack
        itself is untouched; unknown or wrongly typed fields are refused. Verifies
        by reading the file back. Raises KeyError if unknown."""
        kept = _loop_of(loop, strict=True)
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"no agent called {name!r}")
        pack, disabled, _ = self._load(path)
        face = self._stored_face(path)
        self._write(pack, disabled=disabled, face={"emoji": face} if face else None, loop=kept)
        back, _, saved = self._load(path)   # verify: read it back
        if back.as_dict() != pack.as_dict() or saved != kept:
            raise PackError(f"agent {name!r}'s loop didn't store correctly")
        return saved

    # ---- surfaces: where an agent can start (laptop, phone, or both) -----------
    def surface(self, name: str) -> str:
        """This pack's Runs-on setting. Raises KeyError if unknown."""
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"no agent called {name!r}")
        self._load(path)   # re-validates the pack, like every other load
        return self._stored_surface(path) or DEFAULT_SURFACE

    def set_surface(self, name: str, surface: str) -> str:
        """Keep a new Runs-on setting. No approval: it spends nothing and only
        decides where the agent can start. Verifies by reading the file back."""
        validate_surface(surface)
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"no agent called {name!r}")
        pack, disabled, loop = self._load(path)
        face = self._stored_face(path)
        self._write(pack, disabled=disabled, face={"emoji": face} if face else None,
                    loop=loop, surface=surface)
        back, _, _ = self._load(path)   # verify: read it back
        if back.as_dict() != pack.as_dict() or self._stored_surface(path) != surface:
            raise PackError(f"agent {name!r}'s surface didn't store correctly")
        return surface

    def export(self, name: str) -> bytes:
        """The exact pack file, to share. Faces stay on this laptop: the export
        holds the data-only pack, never the emoji or the icon file."""
        return canonical_bytes(self.get(name).as_dict())

    # ---- faces: how an agent looks in the avatar window while it runs ------------
    def face(self, name: str) -> dict | None:
        """`{"emoji": str}` or `{"icon": True}`, else None. Raises KeyError if unknown."""
        self.get(name)   # KeyError if not installed
        if (self.dir / f"{name}{FACE_ICON_SUFFIX}").exists():
            return {"icon": True}
        emoji = self._stored_face(self._path(name))
        return {"emoji": emoji} if emoji else None

    def set_face_emoji(self, name: str, emoji: str) -> dict:
        """Keep a short emoji as this agent's face. No model call, no approval."""
        pack = self.get(name)   # KeyError if not installed
        face = {"emoji": validate_emoji(emoji)}
        self._write(pack, disabled=pack.disabled, face=face, loop=self.loop_state(name))
        return face

    def save_face_icon(self, name: str, image: bytes) -> dict:
        """Keep Muse Image's icon (`<name>.webp`) as this agent's face. Verifies on disk."""
        self.get(name)   # KeyError if not installed
        if not image:
            raise PackError(f"no icon was drawn for agent {name!r}")
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{name}{FACE_ICON_SUFFIX}"
        path.write_bytes(image)
        if path.read_bytes() != image:   # verify: read it back
            raise PackError(f"agent {name!r}'s icon didn't store correctly")
        return {"icon": True}

    def face_path(self, name: str) -> Path:
        """The icon file, for serving. Raises KeyError on a bad name or a missing file."""
        if not isinstance(name, str) or not NAME.match(name):
            raise PackError(f"no agent called {name!r}")
        base = self.dir.resolve() if self.dir.exists() else self.dir
        path = (self.dir / f"{name}{FACE_ICON_SUFFIX}").resolve()
        if path.parent != base or not path.exists():
            raise KeyError(f"no icon for agent {name!r}")
        return path


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



