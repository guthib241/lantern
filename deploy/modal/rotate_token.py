"""Make a new access token for the private Glimmer server, without ever printing it.

    python deploy/modal/rotate_token.py              # new token -> Modal Secret + ~/.lantern/private_token
    python deploy/modal/rotate_token.py --redeploy   # same, then redeploy so running containers use it

What it does:
  1. secrets.token_urlsafe(32) (43 characters, 256 bits)
  2. writes it to a temporary file next to the token file (mode 600)
  3. stores it in the Modal Secret `lantern-private` as LANTERN_PRIVATE_TOKEN
     (creates the Secret the first time, updates it after that)
  4. only then replaces the token file, so a failed Modal call leaves the old token in place

Containers already running keep the old token until they restart: pass --redeploy, or
run `modal deploy deploy/modal/glimmer_app.py` yourself.

Needs Modal credentials (`modal token new`, or MODAL_TOKEN_ID / MODAL_TOKEN_SECRET).
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import glimmer_config as gc  # noqa: E402

APP_FILE = Path(__file__).resolve().parent / "glimmer_app.py"


def new_token() -> str:
    return gc.validate_token(secrets.token_urlsafe(32))


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` readable by the owner only (the folder too)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass   # e.g. Windows; the file mode below is what matters
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, 0o600)


def push_secret(token: str, modal_module=None) -> str:
    """Create or update the Modal Secret. Returns "created" or "updated"."""
    modal = modal_module or __import__("modal")
    try:
        secret = modal.Secret.from_name(gc.SECRET_NAME)
        secret.hydrate()
    except modal.exception.NotFoundError:
        modal.Secret.objects.create(gc.SECRET_NAME, {gc.TOKEN_KEY: token})
        return "created"
    secret.update({gc.TOKEN_KEY: token})
    return "updated"


def rotate(token_file: Path, modal_module=None) -> str:
    token = new_token()
    tmp = token_file.with_name(token_file.name + ".new")
    write_private(tmp, token)
    try:
        action = push_secret(token, modal_module)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, token_file)
    return action


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--token-file", default=gc.DEFAULT_TOKEN_FILE, help="where to save it (default %(default)s)")
    ap.add_argument("--redeploy", action="store_true", help="run `modal deploy` afterwards")
    args = ap.parse_args(argv)
    token_file = Path(args.token_file).expanduser()
    action = rotate(token_file)
    print(f"New token {action} in Modal Secret '{gc.SECRET_NAME}' and saved to {token_file} (owner-only).")
    if args.redeploy:
        return subprocess.call([sys.executable, "-m", "modal", "deploy", str(APP_FILE)])
    print("Running containers keep the old token until they restart; redeploy to switch now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
