"""Encryption at rest for Lantern's memory and audit log (Fernet: AES-128-CBC + HMAC-SHA256).

The key lives in the OS keychain (Windows Credential Locker, macOS Keychain, Secret Service on
Linux) via `keyring`. Where no keychain is available it falls back to ~/.lantern/key (file mode 600).
LANTERN_KEY_STORE=keyring|file|auto picks (default auto).

Encrypted values are stored as "enc1:<token>". Values without the prefix are old plaintext rows,
read as-is and encrypted by Memory's one-time migration.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc1:"
SERVICE, ACCOUNT = "lantern", "memory-key"


class CryptoError(RuntimeError):
    pass


class Cipher:
    def __init__(self, key: bytes, where: str = "memory"):
        self._f = Fernet(key)
        self.where = where        # "keychain" or the key file path, for the UI

    def encrypt(self, text: str | None) -> str | None:
        if text is None or text.startswith(PREFIX):
            return text
        return PREFIX + self._f.encrypt(text.encode()).decode()

    def decrypt(self, value: str | None) -> str | None:
        if value is None or not value.startswith(PREFIX):
            return value
        try:
            return self._f.decrypt(value[len(PREFIX):].encode()).decode()
        except InvalidToken as e:
            raise CryptoError("this memory was encrypted with a different key") from e


def _from_keyring() -> bytes | None:
    try:
        import keyring
        key = keyring.get_password(SERVICE, ACCOUNT)
        if not key:
            key = Fernet.generate_key().decode()
            keyring.set_password(SERVICE, ACCOUNT, key)
            if keyring.get_password(SERVICE, ACCOUNT) != key:
                return None
        return key.encode()
    except Exception:  # no keyring module, no backend, locked keychain
        return None


def _from_file(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def load_cipher(home: str | Path, store: str | None = None) -> Cipher:
    """The installation's cipher: keychain first (auto), else the key file."""
    store = (store or os.environ.get("LANTERN_KEY_STORE", "auto")).lower()
    key_file = Path(home).expanduser() / "key"
    if store in ("auto", "keyring") and not key_file.exists():
        key = _from_keyring()
        if key:
            return Cipher(key, "keychain")
        if store == "keyring":
            raise CryptoError("no usable OS keychain; set LANTERN_KEY_STORE=file")
    return Cipher(_from_file(key_file), str(key_file))


def cipher_for(cfg) -> Cipher | None:
    """The cipher for this configuration: the key sits next to memory.db (or in the keychain)."""
    if not getattr(cfg, "encrypt", True):
        return None
    return load_cipher(Path(cfg.db_path).expanduser().parent)
