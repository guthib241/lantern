"""Make Lantern's face with Muse Image: one canonical character, then each expression anchored on it.

    python scripts/make_avatar.py --dry-run                        # show the prompts, no API call
    python scripts/make_avatar.py                                  # 7 images, about $0.07
    python scripts/make_avatar.py --canonical ~/Downloads/lantern.png   # start from your MetaMuse art
    python scripts/make_avatar.py --only smug,annoyed              # redo some, anchored on the last canonical
    python scripts/make_avatar.py --brief "a tiny brass lantern robot with a teal flame"

Writes desktop/assets/avatar/<expression>.webp + manifest.json/.js. Every call goes through
Lantern's audited client, and the cost is logged to memory like any other model call.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lantern import config as config_mod  # noqa: E402
from lantern import models  # noqa: E402
from lantern.audit import AuditLog, GuardedClient  # noqa: E402
from lantern.avatar import EXPRESSIONS, AvatarError, generate, plan  # noqa: E402
from lantern.memory import Memory  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "desktop" / "assets" / "avatar"


def main(argv: list[str] | None = None, client=None) -> int:
    p = argparse.ArgumentParser(prog="make_avatar")
    p.add_argument("--canonical", help="your own character image (PNG/JPEG/WebP), e.g. made in the MetaMuse app")
    p.add_argument("--brief", help="describe the character instead of Lantern's default brief")
    p.add_argument("--only", help="comma list of expressions: " + ",".join(EXPRESSIONS))
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    only = [e.strip() for e in a.only.split(",")] if a.only else None
    reference = None
    if a.canonical:
        path = Path(a.canonical).expanduser()  # a file you chose on the command line
        reference = (path.read_bytes(), path.suffix)
    try:
        turns = plan(brief=a.brief, from_reference=reference is not None, only=only, have_canonical=bool(only))
    except AvatarError as e:
        print(f"✗ {e}")
        return 2
    if a.dry_run:
        for t in turns:
            print(f"[{t.expression}] {'(chained on the canonical) ' if t.chained else ''}{t.prompt}\n")
        print(f"{len(turns)} images · about ${models.image_cost(len(turns)):.2f}")
        return 0

    cfg = config_mod.load()
    if client is None:
        key = os.environ.get(models.API_KEY_ENV)
        if not key:
            print(f"✗ set {models.API_KEY_ENV} first (or use --dry-run)")
            return 2
        from openai import OpenAI
        client = OpenAI(base_url=cfg.base_url, api_key=key, timeout=180, max_retries=2)
    audited = GuardedClient(client, AuditLog(cfg.audit_path), destination=cfg.base_url)
    memory = Memory(cfg.db_path)
    try:
        sheet = generate(audited, a.out, brief=a.brief, reference=reference, only=only,
                         on_cost=lambda usd: memory.log_usage(None, models.IMAGE, cost_usd=usd),
                         on_progress=lambda e: print(f"✓ {e}"))
    except AvatarError as e:
        print(f"✗ {e}")
        return 1
    print(f"Saved {len(sheet.made)} images to {a.out} · ${sheet.cost_usd:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
