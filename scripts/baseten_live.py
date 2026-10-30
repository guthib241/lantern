"""Check Muse Spark Open on Baseten against the LIVE endpoint, and report. Costs real money (cents).

    LANTERN_BASETEN=on BASETEN_API_KEY=injected-by-proxy python scripts/baseten_live.py
    python scripts/baseten_live.py --only text      # or: image

Checks (each one request through lantern/baseten.py, the same code the product uses):
  text   "What is 17 * 23?" -> the answer has 391; the reply holds no reasoning; tokens and cost measured
  image  a rendered receipt (PNG, made here, no user file) -> the total "42.50" is read from the picture

Prints a JSON report: per check ok, seconds, input/output/reasoning tokens and the cost from
models.token_cost (reasoning is billed inside the output tokens). Never prints the key.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lantern import baseten  # noqa: E402


def receipt_png() -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (420, 260), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(["CORNER CAFE", "Tea          2 x 4.00", "Sandwich        34.50", "",
                              "TOTAL           42.50"]):
        d.text((30, 30 + 40 * i), line, fill="black")
    buf = io.BytesIO()
    img.resize((840, 520)).save(buf, "PNG")
    return buf.getvalue()


def run(sp: baseten.SparkOpen, name: str) -> dict:
    t = time.monotonic()
    try:
        if name == "text":
            r = sp.ask("Answer with the number only.", "What is 17 * 23?")
            ok, want = "391" in r.text, "391"
        else:
            r = sp.ask("Answer in one short line.", "What is the TOTAL on this receipt?", images=[receipt_png()])
            ok, want = "42.50" in r.text, "42.50"
    except Exception as e:   # report it; the key is never part of an error here
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300], "s": round(time.monotonic() - t, 1)}
    return {"ok": ok, "want": want, "answer": r.text[:200], "s": round(time.monotonic() - t, 1),
            "input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens,
            "reasoning_tokens": r.usage.reasoning_tokens, "cost_usd": round(r.cost_usd, 6)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", nargs="*", choices=["text", "image"], default=["text", "image"])
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    sp = baseten.from_env(dict(os.environ))
    if sp is None:
        print(baseten.BasetenConfig.from_env(dict(os.environ)).why_not(), file=sys.stderr)
        return 2
    report = {name: run(sp, name) for name in args.only}
    report["total_cost_usd"] = round(sum(c.get("cost_usd", 0.0) for c in report.values()), 6)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0 if all(c["ok"] for k, c in report.items() if k != "total_cost_usd") else 1


if __name__ == "__main__":
    raise SystemExit(main())
