"""Decoy screens (S7): the cloud model sees stand-in values, and only this computer types the real ones.

In `LANTERN_SCREENSHOTS=decoy` mode, before a screenshot leaves for the computer-use model, every place one of the
user's own values shows in Lantern's browser (a field's value, or text on the page) is repainted with a stand-in:
the page's own element shows the fake value for the moment of the screenshot, so the font, size and position match
exactly, then the real value is put back. The goal the model gets names the same stand-ins. When the model types,
the actuator here swaps each stand-in for the real value at keystroke time. Typing is refused when:
  - the text contains one of the real values (the model was never shown them, so it came from somewhere else), or
  - the field it goes into is one Lantern listed with a private value, and the text isn't that field's stand-in.
A stand-in keeps the value's shape (digits stay digits, capitals stay capitals, separators stay), is the same every
time in one task (an HMAC of the value with a per-task secret), never equals the real value and never contains one.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import string

MIN_LEN = 3
UPPER, LOWER, DIGITS = string.ascii_uppercase, string.ascii_lowercase, string.digits


class DecoyError(ValueError):
    """Typing refused; the message is safe to show."""


def _stream(key: bytes, value: str, n: int):
    """Deterministic bytes for `value` (HMAC-SHA256 in counter mode)."""
    block = 0
    while True:
        for b in hmac.new(key, f"{n}:{block}:{value}".encode(), hashlib.sha256).digest():
            yield b
        block += 1


def stand_in(key: bytes, value: str, n: int = 0) -> str:
    """The value with every letter and digit replaced by another of the same kind; everything else kept."""
    out, rnd = [], _stream(key, value, n)
    for ch in value:
        pool = UPPER if ch in UPPER else LOWER if ch in LOWER else DIGITS if ch in DIGITS else None
        if pool is None:
            out.append(ch)
            continue
        while True:   # never the same character in the same place
            c = pool[next(rnd) % len(pool)]
            if c != ch:
                break
        out.append(c)
    return "".join(out)


class Decoys:
    """The stand-ins for one task. `values`: the user's own values that mustn't reach a cloud model."""

    def __init__(self, values: list[str], key: bytes | None = None):
        self.key = key or secrets.token_bytes(32)
        reals = sorted({str(v).strip() for v in values if len(str(v).strip()) >= MIN_LEN}, key=len, reverse=True)
        self.fake: dict[str, str] = {}
        taken = set(reals)
        for real in reals:
            n = 0
            while True:
                f = stand_in(self.key, real, n)
                if f not in taken and not any(r.lower() in f.lower() for r in reals) and \
                        not any(f.lower() in t.lower() or t.lower() in f.lower() for t in self.fake.values()):
                    break
                n += 1
            self.fake[real] = f
            taken.add(f)
        self.real = {f: r for r, f in self.fake.items()}
        self.fields: dict[str, str] = {}     # field label (lowercase) -> the stand-in that alone may be typed there
        self.swapped = 0

    def __bool__(self) -> bool:
        return bool(self.fake)

    def hide(self, text: str) -> str:
        """Text with every real value replaced by its stand-in (for what the model reads)."""
        for real in self.fake:   # longest first
            text = _replace_ci(text, real, self.fake[real])
        return text

    def guard(self, label: str, real_value: str) -> str:
        """Note that the field `label` gets `real_value`; returns what the model is told to type there."""
        f = self.fake.get(str(real_value).strip())
        if f is None:
            return str(real_value)
        if label.strip():
            self.fields[label.strip().lower()] = f
        return f

    def reveal(self, text: str, field_label: str = "") -> str:
        """What is really typed for the model's `text`; DecoyError when it must not be typed at all."""
        low = text.lower()
        for real in self.fake:
            if real.lower() in low:
                raise DecoyError("it typed one of your real values, which it was never shown")
        want = self.fields.get(field_label.strip().lower()) if field_label else None
        if want is not None and text.strip() != want:
            raise DecoyError("only the stand-in Lantern gave for this field may be typed into it")
        out = text
        for f in sorted(self.real, key=len, reverse=True):
            if f in out:
                self.swapped += out.count(f)
                out = out.replace(f, self.real[f])
        return out

    def pairs(self) -> list[list[str]]:
        """[real, stand-in] for the page script that repaints the screenshot (stays on this computer)."""
        return [[r, f] for r, f in self.fake.items()]


def _replace_ci(text: str, old: str, new: str) -> str:
    """Case-insensitive replace (the page may show a value in another case)."""
    if not old:
        return text
    out, low, lold, i = [], text.lower(), old.lower(), 0
    while True:
        j = low.find(lold, i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:j])
        out.append(new)
        i = j + len(old)
