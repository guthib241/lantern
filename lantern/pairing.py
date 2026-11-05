"""Pair a phone: one 6-digit code on the desktop, one device token on the phone.

Flow: desktop calls POST /pair/start (desktop token) and shows the code;
the phone calls POST /pair {code, name} once and stores the device token.
The phone then sends `X-Lantern-Device` on every /m/api call.

Only SHA-256 hashes of device tokens (and of the pairing code) are stored,
in `<home>/devices.json` (mode 0600). The code is single use, expires after
5 minutes, and is burned after 5 wrong tries. Comparisons use
secrets.compare_digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

CODE_TTL = 300
MAX_TRIES = 5


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _new_id() -> str:
    return secrets.token_hex(4)


class PairingError(ValueError):
    """Wrong code, expired code, burned code, unknown device..."""


class PairingStore:
    """Pairing codes + paired devices. `path` is devices.json; `clock` is injectable for tests."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] | None = None,
                 code_gen: Callable[[], str] | None = None,
                 token_gen: Callable[[], str] | None = None) -> None:
        self.path = Path(path).expanduser()
        self.clock = clock or time.time
        self._code_gen = code_gen or _new_code
        self._token_gen = token_gen or _new_token
        self._data = {"devices": [], "pairing": None}
        self._load()

    # ---- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        if isinstance(raw, dict):
            devs = raw.get("devices")
            self._data["devices"] = devs if isinstance(devs, list) else []
            pairing = raw.get("pairing")
            self._data["pairing"] = pairing if isinstance(pairing, dict) else None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ---- pairing -------------------------------------------------------------
    def start(self) -> tuple[str, int]:
        """New 6-digit code (replaces any old one). Returns (code, expires_in)."""
        code = self._code_gen()
        now = self.clock()
        self._data["pairing"] = {"code_hash": _sha(code), "expires_at": now + CODE_TTL, "attempts": 0}
        self._save()
        return code, CODE_TTL

    def _pairing_active(self) -> dict | None:
        p = self._data.get("pairing")
        if not isinstance(p, dict):
            return None
        if self.clock() > float(p.get("expires_at", 0)):
            self._data["pairing"] = None
            self._save()
            return None
        return p

    def redeem(self, code: str, name: str) -> tuple[str, str]:
        """Trade a code for (device_token, device_id). Single use; 5 wrong tries burn the code."""
        p = self._data.get("pairing")
        if not isinstance(p, dict):
            raise PairingError("no pairing code is active; start one on the desktop first")
        if self.clock() > float(p.get("expires_at", 0)):
            self._data["pairing"] = None
            self._save()
            raise PairingError("that code has expired; start a new one on the desktop")
        if int(p.get("attempts", 0)) >= MAX_TRIES:
            self._data["pairing"] = None
            self._save()
            raise PairingError("too many wrong tries; start a new code on the desktop")
        given = _sha(str(code or "").strip())
        if not secrets.compare_digest(given, str(p.get("code_hash", ""))):
            p["attempts"] = int(p.get("attempts", 0)) + 1
            if p["attempts"] >= MAX_TRIES:
                self._data["pairing"] = None  # burned
            self._save()
            raise PairingError("that code is not right")
        # Right: single use, then a device token (only its hash is stored).
        self._data["pairing"] = None
        token = self._token_gen()
        device_id = _new_id()
        label = " ".join(str(name or "").split())[:60] or "phone"
        self._data["devices"].append({"id": device_id, "name": label,
                                      "token_hash": _sha(token),
                                      "created_at": self.clock(), "revoked": False})
        self._save()
        return token, device_id

    # ---- devices ---------------------------------------------------------------
    def verify(self, token: str) -> dict | None:
        """The device for a phone token, or None (unknown or revoked)."""
        if not token:
            return None
        want = _sha(token)
        for d in self._data.get("devices", []):
            if not isinstance(d, dict) or d.get("revoked"):
                continue
            if secrets.compare_digest(str(d.get("token_hash", "")), want):
                return d
        return None

    def list(self) -> list[dict]:
        """Paired devices without hashes: [{id, name, created_at, revoked}]."""
        out = []
        for d in self._data.get("devices", []):
            if not isinstance(d, dict):
                continue
            out.append({k: d.get(k) for k in ("id", "name", "created_at", "revoked")})
        return out

    def revoke(self, device_id: str) -> dict:
        """Revoke one device. Raises KeyError if unknown, ValueError if already revoked."""
        for d in self._data.get("devices", []):
            if isinstance(d, dict) and d.get("id") == device_id:
                if d.get("revoked"):
                    raise ValueError(f"device {device_id} is already revoked")
                d["revoked"] = True
                self._save()
                return {"id": device_id, "revoked": True}
        raise KeyError(f"no device {device_id}")
