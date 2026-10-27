"""Labels and handles (S0): where every value came from, and content the planner may pass on but never read.

  Label        {integrity: trusted | untrusted, confidentiality: public | private | secret, sources}
               Labels join when values combine: untrusted wins over trusted, the stricter confidentiality wins.
  Handles      one task's store of content the planner is not shown. `$doc1` holds a document as it came in
               (a file's text, a web page's confirmation, another program's result); `$h2` holds a reader's
               text answer or a private-lane answer. The planner sees only the names. It may write them in
               tool arguments (they are filled in locally, then checked against the risk tiers) or in its reply
               (filled in on the user's own screen, never sent back to the model).
  deny()       the values that must never reach a cloud model: everything private or secret. The audited
               client refuses any request that contains one (audit.py), in code.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

INTEGRITY = ("trusted", "untrusted")
CONFIDENTIALITY = ("public", "private", "secret")
HANDLE = re.compile(r"\$(?:doc|h)\d+\b")
MIN_DENY = 6   # shorter private strings (a "yes", a year) are too common to block on


@dataclass(frozen=True)
class Label:
    integrity: str = "trusted"
    confidentiality: str = "public"
    sources: tuple[str, ...] = ()

    def __post_init__(self):
        if self.integrity not in INTEGRITY or self.confidentiality not in CONFIDENTIALITY:
            raise ValueError(f"bad label {self.integrity}/{self.confidentiality}")

    @property
    def untrusted(self) -> bool:
        return self.integrity == "untrusted"

    @property
    def confidential(self) -> bool:
        return self.confidentiality != "public"

    def join(self, other: "Label") -> "Label":
        integrity = "untrusted" if self.untrusted or other.untrusted else "trusted"
        conf = max(self.confidentiality, other.confidentiality, key=CONFIDENTIALITY.index)
        return Label(integrity, conf, tuple(dict.fromkeys(self.sources + other.sources)))

    def public(self) -> dict:
        return {"integrity": self.integrity, "confidentiality": self.confidentiality, "sources": list(self.sources)}

    @classmethod
    def load(cls, d: dict) -> "Label":
        return cls(d.get("integrity", "trusted"), d.get("confidentiality", "public"), tuple(d.get("sources") or ()))


TRUSTED = Label()


def untrusted(source: str, confidentiality: str = "public") -> Label:
    return Label("untrusted", confidentiality, (source,))


class Handles:
    def __init__(self):
        self._items: dict[str, dict] = {}
        self._n = {"doc": 0, "h": 0}
        self._lock = threading.Lock()

    # ---- storing ---------------------------------------------------------------------------------
    def put(self, value: str, label: Label, kind: str = "h", meta: dict | None = None) -> str:
        with self._lock:
            self._n[kind] += 1
            name = f"${kind}{self._n[kind]}"
            self._items[name] = {"value": str(value), "label": label, "meta": dict(meta or {})}
        return name

    def get(self, name: str) -> tuple[str, Label] | None:
        item = self._items.get(str(name).strip())
        return None if item is None else (item["value"], item["label"])

    def meta(self, name: str) -> dict:
        return dict((self._items.get(name) or {}).get("meta") or {})

    def names(self) -> list[str]:
        return list(self._items)

    # ---- filling in -----------------------------------------------------------------------------------
    def expand(self, obj: Any, *, private_as: str | None = None) -> tuple[Any, Label, list[str]]:
        """Fill handles into every string of `obj`. Returns (filled, joined label, handles used).
        With `private_as`, private and secret handles become that text instead (for a public copy)."""
        used: list[str] = []
        label = TRUSTED

        def one(m: re.Match) -> str:
            nonlocal label
            item = self._items.get(m.group(0))
            if item is None:
                return m.group(0)
            used.append(m.group(0))
            label = label.join(item["label"])
            if private_as is not None and item["label"].confidential:
                return private_as
            return item["value"]

        def walk(x: Any) -> Any:
            if isinstance(x, str):
                return HANDLE.sub(one, x)
            if isinstance(x, dict):
                return {k: walk(v) for k, v in x.items()}
            if isinstance(x, list):
                return [walk(v) for v in x]
            return x
        return walk(obj), label, list(dict.fromkeys(used))

    def rehandle(self, obj: Any) -> Any:
        """The other way: replace private or untrusted text that came from a handle with the handle's name,
        so a tool result never shows the planner what it isn't allowed to read."""
        pairs = sorted(((i["value"], n) for n, i in self._items.items()
                        if (i["label"].confidential or i["label"].untrusted) and len(i["value"]) >= 4),
                       key=lambda p: len(p[0]), reverse=True)
        if not pairs:
            return obj

        def walk(x: Any) -> Any:
            if isinstance(x, str):
                for value, name in pairs:
                    if value in x:
                        x = x.replace(value, name)
                return x
            if isinstance(x, dict):
                return {k: walk(v) for k, v in x.items()}
            if isinstance(x, list):
                return [walk(v) for v in x]
            return x
        return walk(obj)

    def deny(self) -> list[str]:
        """Strings that may never reach a cloud model: private and secret values, and each longer line of them."""
        out: list[str] = []
        for item in list(self._items.values()):
            if not item["label"].confidential:
                continue
            value = item["value"].strip()
            if len(value) >= MIN_DENY:
                out.append(value)
            out += [line.strip() for line in value.splitlines() if len(line.strip()) >= 16]
        return list(dict.fromkeys(out))

    # ---- checkpoints (taskrun.py keeps them in the encrypted task state) ------------------------------
    def dump(self) -> dict:
        with self._lock:
            return {"n": dict(self._n), "items": {k: {"value": v["value"], "label": v["label"].public(),
                                                      "meta": v["meta"]} for k, v in self._items.items()}}

    def load(self, data: dict | None) -> None:
        if not data:
            return
        with self._lock:
            self._n.update(data.get("n") or {})
            for k, v in (data.get("items") or {}).items():
                self._items[k] = {"value": v["value"], "label": Label.load(v["label"]), "meta": v.get("meta") or {}}
