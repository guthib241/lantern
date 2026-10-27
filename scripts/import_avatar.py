"""Use art you made in the MetaMuse app (Muse Image) or animated in Vibes / Muse Video as Lantern's face.

    python scripts/import_avatar.py ~/Downloads/lantern-muse/      # files named after expressions:
                                                                    # idle.png, thinking.mp4, celebrating.webp ...
    python scripts/import_avatar.py ~/Downloads/wave.mp4 --as celebrating

Images (PNG/JPEG/WebP) are kept as they are (JPEG becomes PNG). Videos (MP4/MOV/WebM) become a
square, silent, looping 360x360 WebM (VP9, at most 6 s) with ffmpeg. Expressions you don't import
fall back to idle in the desktop app. Updates desktop/assets/avatar/manifest.json/.js.
Tip: make the other expressions from your idle art with Muse Image:
    python scripts/make_avatar.py --canonical ~/Downloads/lantern-muse/idle.png
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lantern.avatar import EXPRESSIONS, read_manifest, write_manifest  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "desktop" / "assets" / "avatar"
IMAGES = {".png", ".webp", ".jpg", ".jpeg"}
VIDEOS = {".mp4", ".mov", ".webm", ".m4v"}
SOURCE = "imported from the MetaMuse app / Vibes (made with Muse)"


def ffmpeg(*args: str) -> None:
    if not shutil.which("ffmpeg"):
        raise SystemExit("✗ ffmpeg is needed for videos and JPEGs (install it, then retry)")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, timeout=300)


def import_one(src: Path, expression: str, out: Path) -> str:
    """Copy or convert one file; returns the file name written in `out`."""
    ext = src.suffix.lower()
    out.mkdir(parents=True, exist_ok=True)
    if ext in (".png", ".webp"):
        name = f"{expression}{ext}"
        (out / name).write_bytes(src.read_bytes())
    elif ext in (".jpg", ".jpeg"):
        name = f"{expression}.png"
        ffmpeg("-i", str(src), str(out / name))
    elif ext in VIDEOS:
        name = f"{expression}.webm"
        ffmpeg("-i", str(src), "-t", "6", "-an",
               "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=360:360:flags=lanczos,fps=24",
               "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34", "-row-mt", "1", str(out / name))
    else:
        raise SystemExit(f"✗ {src.name}: use PNG, JPEG, WebP, MP4, MOV or WebM")
    return name


def collect(paths: list[Path], as_expression: str | None) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for p in paths:
        if p.is_dir():
            for f in sorted(p.glob("*")):
                if f.stem in EXPRESSIONS and f.suffix.lower() in IMAGES | VIDEOS:
                    found[f.stem] = f
        elif as_expression:
            found[as_expression] = p
        elif p.stem in EXPRESSIONS:
            found[p.stem] = p
        else:
            raise SystemExit(f"✗ {p.name}: name it after an expression ({', '.join(EXPRESSIONS)}) or pass --as")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="import_avatar")
    ap.add_argument("paths", nargs="+", help="a folder of expression files, or single files")
    ap.add_argument("--as", dest="as_expression", choices=list(EXPRESSIONS), help="expression for a single file")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    out = Path(a.out)
    files = collect([Path(p).expanduser() for p in a.paths], a.as_expression)
    if not files:
        print("✗ nothing to import: name files after expressions, e.g. idle.png, thinking.mp4")
        return 2
    manifest = read_manifest(out)
    entries = dict(manifest.get("expressions", {}))
    for expression, src in files.items():
        name = import_one(src, expression, out)
        entries[expression] = {"file": name, "source": SOURCE, "original": src.name}
        print(f"✓ {expression} ← {src.name}")
    meta = {k: v for k, v in manifest.items() if k != "expressions"}
    write_manifest(out, entries, {**meta, "made_with": meta.get("made_with", "Muse"), "updated_at": time.time()})
    missing = [e for e in EXPRESSIONS if e not in entries]
    if missing:
        print(f"Missing (they'll show idle): {', '.join(missing)}. "
              "Make them with Muse Image: python scripts/make_avatar.py --canonical <your idle image>")
    return 0


if __name__ == "__main__":
    from lantern.cli import safe_console
    safe_console()   # ✓ and ✗ must not crash a piped Windows console
    raise SystemExit(main())
