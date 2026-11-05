"""The profile vault: the boring answers forms keep asking for, stored only on this computer.

~/.lantern/profile.json (file mode 600):
  {
    "profile": {"full name": "Asha Rao", "email": "asha@example.com", "phone": "+91 98xxxxxx",
                "address": "...", "education": "...", "github": "https://github.com/..."},
    "answers": {"why do you want to join?": "..."},
    "forms":   {"hackathon reimbursement": "https://..."}
  }

It never stores passwords, one-time codes, card numbers or ID numbers: keys that
name them and values that look like them are refused in code. This file is
Lantern's own (under ~/.lantern), so it is read directly, not through policy.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .guards import text_reason, value_reason

SECTIONS = ("profile", "answers", "forms")


class VaultError(ValueError):
    """The value can't go into the vault."""


class Vault:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def load(self) -> dict:
        if not self.path.exists():
            return {s: {} for s in SECTIONS}
        data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        return {s: dict(data.get(s) or {}) for s in SECTIONS}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({s: data.get(s, {}) for s in SECTIONS}, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(self.path, 0o600)

    def set(self, key: str, value: str, section: str = "profile") -> None:
        if section not in SECTIONS:
            raise VaultError(f"unknown section {section!r}; use one of {SECTIONS}")
        key, value = key.strip(), str(value).strip()
        if not key:
            raise VaultError("key is empty")
        reason = text_reason(key) if section != "forms" else None
        if reason:
            raise VaultError(f"Lantern never stores a {reason}; you type those yourself")
        reason = value_reason(value)
        if reason:
            raise VaultError(f"not saved: {reason}. Lantern never stores those")
        data = self.load()
        data[section][key] = value
        self.save(data)

    def remove(self, key: str, section: str = "profile") -> bool:
        data = self.load()
        existed = data.get(section, {}).pop(key, None) is not None
        if existed:
            self.save(data)
        return existed

    def for_model(self) -> dict:
        """What the form planner sees: profile and reusable answers (never form bookmarks)."""
        data = self.load()
        return {"profile": data["profile"], "answers": data["answers"]}

    def bookmarks(self) -> dict:
        return self.load()["forms"]
