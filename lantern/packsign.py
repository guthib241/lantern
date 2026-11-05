"""Signed agent packs: who made a shared agent, and whether it changed since.

One Ed25519 key per user in `<home>/keys/pack-signing.key` (file mode 600).
The private key never leaves the laptop and is never logged: only the public
key and the signature travel inside shared pack files.

The signature covers `agentpacks.canonical_bytes` of the pack without the
`signature` field, so exports round-trip byte-identical and stay verifiable.
Trust is on first use: `trusted_keys.json` in the agents folder maps a key
fingerprint to the author name it was first seen with.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

SIGNED, UNSIGNED, TAMPERED = "signed", "unsigned", "tampered"

KEY_DIRNAME = "keys"
KEY_FILENAME = "pack-signing.key"
TRUSTED_FILENAME = "trusted_keys.json"
SIGNATURE_ALG = "ed25519"

PUB_BYTES = 32   # an Ed25519 public key is 32 raw bytes
SIG_BYTES = 64   # an Ed25519 signature is 64 raw bytes


class PackSignError(ValueError):
    """The signing key or a pack signature is unusable."""


def key_path(home: str | Path) -> Path:
    """`<home>/keys/pack-signing.key`: one key per user on this laptop."""
    return Path(home).expanduser() / KEY_DIRNAME / KEY_FILENAME


def _public_b64(private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(private_key.public_key().public_bytes_raw()).decode()


def _cipher_for(home: Path):
    """Lantern's at-rest cipher for this home, or None when unavailable or off."""
    try:
        from .crypto import cipher_for
        from types import SimpleNamespace as NS
        return cipher_for(NS(encrypt=True, db_path=str(Path(home) / "memory.db")))
    except Exception:
        return None


def _write_key_file(path: Path, private_key: Ed25519PrivateKey) -> None:
    """Create the key file owner-only in one step: never readable by others."""
    raw_b64 = base64.b64encode(private_key.private_bytes_raw()).decode()
    cipher = _cipher_for(path.parent.parent)
    text = cipher.encrypt(raw_b64) if cipher else raw_b64
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return   # another Lantern process made it a moment ago; the caller reads it back
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _read_key_file(path: Path) -> Ed25519PrivateKey:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("enc1:"):
        cipher = _cipher_for(path.parent.parent)
        if cipher is None:
            raise PackSignError("the signing key is encrypted but no cipher is available")
        try:
            text = cipher.decrypt(text)
        except Exception as e:
            raise PackSignError("the signing key can't be decrypted on this computer") from e
    try:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(text, validate=True))
    except (binascii.Error, ValueError) as e:
        raise PackSignError("the signing key file is broken; delete it to make a new one") from e


def ensure_key(home: str | Path) -> tuple[Ed25519PrivateKey, str]:
    """Load this user's signing key, creating it once when missing.

    Returns the private key and its base64 public key. The private key is
    never logged; only the public half leaves this function."""
    path = key_path(home)
    if not path.exists():
        _write_key_file(path, Ed25519PrivateKey.generate())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass   # Windows: the file is already in Lantern's own folder
    private_key = _read_key_file(path)
    return private_key, _public_b64(private_key)


def fingerprint(public_key_b64: str) -> str:
    """The key's short name: first 8 hex chars of its SHA-256, as `3F2A-91C0`."""
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
    except binascii.Error as e:
        raise PackSignError(f"bad public key: {e}") from e
    if len(raw) != PUB_BYTES:
        raise PackSignError("bad public key: not 32 bytes")
    hexed = hashlib.sha256(raw).hexdigest()[:8].upper()
    return f"{hexed[:4]}-{hexed[4:]}"


def _unsigned(pack_dict: dict) -> dict:
    return {k: v for k, v in pack_dict.items() if k != "signature"}


def sign(pack_dict: dict, private_key: Ed25519PrivateKey) -> dict:
    """Add this laptop's signature to a pack dict. Signs the canonical bytes
    of the pack without any previous `signature` field."""
    from .agentpacks import canonical_bytes
    body = canonical_bytes(_unsigned(pack_dict))
    sig = base64.b64encode(private_key.sign(body)).decode()
    return {**_unsigned(pack_dict),
            "signature": {"alg": SIGNATURE_ALG, "public_key": _public_b64(private_key), "sig": sig}}


def check_shape(sig: Any) -> dict:
    """Strict shape of a `signature` field. Raises PackSignError when malformed."""
    if not isinstance(sig, dict) or set(sig) != {"alg", "public_key", "sig"}:
        raise PackSignError("bad 'signature': needs exactly 'alg', 'public_key' and 'sig'")
    if sig["alg"] != SIGNATURE_ALG:
        raise PackSignError(f"bad 'signature': unknown alg {sig['alg']!r}")
    for field, size in (("public_key", PUB_BYTES), ("sig", SIG_BYTES)):
        value = sig[field]
        if not isinstance(value, str) or not value:
            raise PackSignError(f"bad 'signature': {field!r} must be base64 text")
        try:
            raw = base64.b64decode(value, validate=True)
        except binascii.Error as e:
            raise PackSignError(f"bad 'signature': {field!r} is not base64") from e
        if len(raw) != size:
            raise PackSignError(f"bad 'signature': {field!r} is the wrong length")
    return sig


def verify(pack_dict: dict) -> str:
    """`signed` (good signature), `unsigned` (no signature), or `tampered`
    (the content no longer matches its signature, or the signature is malformed)."""
    if not isinstance(pack_dict, dict) or "signature" not in pack_dict:
        return UNSIGNED
    try:
        sig = check_shape(pack_dict["signature"])
        from .agentpacks import canonical_bytes
        Ed25519PublicKey.from_public_bytes(
            base64.b64decode(sig["public_key"])).verify(
            base64.b64decode(sig["sig"]), canonical_bytes(_unsigned(pack_dict)))
    except (PackSignError, InvalidSignature, binascii.Error, ValueError):
        return TAMPERED
    return SIGNED


def signature_info(pack_dict: dict) -> dict:
    """What the panel badge and the install card show: status, fingerprint
    (when signed), and author. Never raises on a hand-edited file."""
    try:
        status = verify(pack_dict)
    except Exception:
        status = TAMPERED
    info: dict[str, Any] = {"status": status, "fingerprint": None,
                            "author": pack_dict.get("author") if isinstance(pack_dict, dict) else None}
    if status == SIGNED:
        try:
            info["fingerprint"] = fingerprint(pack_dict["signature"]["public_key"])
        except PackSignError:
            info["status"] = TAMPERED
    return info


def trusted_path(agents_dir: str | Path) -> Path:
    """`trusted_keys.json` next to the installed packs: fingerprint -> author."""
    return Path(agents_dir).expanduser() / TRUSTED_FILENAME


def load_trusted(agents_dir: str | Path) -> dict[str, str]:
    """Fingerprints this user already approved, or {} when none (or broken)."""
    try:
        data = json.loads(trusted_path(agents_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def record_trust(agents_dir: str | Path, fp: str, author: str) -> dict[str, str]:
    """Remember that this user trusts fingerprint `fp` as `author`'s key."""
    trusted = load_trusted(agents_dir)
    trusted[fp] = author
    path = trusted_path(agents_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trusted, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return trusted
