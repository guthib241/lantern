"""S14 data: goals → which learned routine (or none), labelled by Muse Wonder, checked, scrubbed, split by routine.

    python scripts/s14_export.py --out demo/s14/data            # generate, label, split ($4.00 cap)
    python scripts/s14_export.py --out demo/s14/data --spark-ref  # also Muse Spark's accuracy on the held-out set

The job is the one `compiler.match` asks a model today: a request plus the list of learned routines (name, title,
examples) → the routine's name, or "none". The prompt is byte-for-byte Lantern's (MATCH_RULES and the same
listing JSON), so a tuned model plugs into the ladder as it is.

  1. Goals (Muse Spark, structured output, one call per routine, cached): paraphrases of what each routine does
     (label: that routine) and near-misses in the same words that ask for something else (label: none). Plus the
     rehearsal story's own requests. Routines come from demo/s14/routines.json: fake, seeded data.
  2. Listings: each goal is asked against its routine and 5 others; 1 paraphrase in 5 is asked without its
     routine (label: none). Training listings use training routines only; the held-out routines are never seen
     in training (split by routine).
  3. Labels (Muse Wonder, the teacher, low effort): kept only when Wonder's answer equals the catalog's label (the
     owner's demo data says so); disagreements are counted and dropped.
  4. Scrubbed: the scrubber runs over every line, and a line with a redaction or anything path-like is dropped.
  5. Written: train.jsonl / heldout.jsonl ({"messages": [system, user, assistant]}, the chat format the
     Glimmer post-training docs use) and report.json (counts, agreement, cost).
Only these JSONL files ever go to Modal (scripts/s14_modal.py). Spend is measured per call and stops at --max-usd.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lantern import models  # noqa: E402
from lantern.compiler import MATCH_RULES, MATCH_SCHEMA  # noqa: E402
from lantern.routines.contract import goal_words  # noqa: E402
from lantern.scrubber import DEFAULT as SCRUBBER  # noqa: E402

CATALOG = Path(__file__).resolve().parent.parent / "demo" / "s14" / "routines.json"
DISTRACTORS = 5
ABSENT_EVERY = 5
SEED = 14

GEN_RULES = """You write test requests for a desktop assistant that has learned routines. You get one routine (its
name, title and example requests). Write:
- same: 10 requests a person might type or say that ask for exactly this routine's job, in varied words and
  lengths, some casual, some with a detail (a date, a name of a shop or a month). Never copy an example.
- near: 6 requests that share words with this routine but ask for a different job it doesn't do (look something
  up, ask a question, do a related but different thing). Each must clearly not be this routine's job.
No real people's names, no email addresses, no file paths, no health or medical topics."""
GEN_SCHEMA = {"type": "object", "properties": {
    "same": {"type": "array", "items": {"type": "string"}},
    "near": {"type": "array", "items": {"type": "string"}}},
    "required": ["same", "near"], "additionalProperties": False}

# The rehearsal story's own requests (lantern/demo.py, scripts/live_story.py) and what they are.
STORY = [("Tidy my Downloads folder.", "tidy-downloads"), ("Make me a water timer.", "none"),
         ("Remind me in 20 minutes to record the demo video.", "none"),
         ("Summarise my meeting minutes in Documents.", "none"), ("What did you do for me today?", "none")]
PATHLIKE = re.compile(r"(?:[A-Za-z]:\\|~/|/home/|/Users/|\\\\)")


class Spend:
    """Measured cost so far, and a hard stop."""

    def __init__(self, cap: float):
        self.cap, self.usd, self.calls = cap, 0.0, 0

    def add(self, model: str, usage: Any) -> None:
        self.usd += models.token_cost(model, models.chat_usage(usage))
        self.calls += 1

    def check(self) -> None:
        if self.usd >= self.cap:
            raise SystemExit(f"stopped: spend reached ${self.usd:.4f} (cap ${self.cap:.2f})")


def load_catalog(path: Path = CATALOG) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def entry(r: dict) -> dict:
    """A routine as compiler._match_by_model lists it (these routines have no choosable params)."""
    return {"name": r["name"], "title": r["title"], "examples": r["examples"][:5], "params": []}


def messages(goal: str, listing: list[dict]) -> list[dict]:
    """Exactly what compiler._match_by_model sends: MATCH_RULES, then the request and the listing as JSON."""
    return [{"role": "system", "content": MATCH_RULES},
            {"role": "user", "content": json.dumps({"request": goal_words(goal), "routines": listing},
                                                   ensure_ascii=False)}]


def answer(name: str) -> str:
    return json.dumps({"routine": name, "params": []})


def clean(text: str) -> bool:
    """Nothing the scrubber would redact, and nothing that looks like a path on someone's computer."""
    return not SCRUBBER.scrub(text).redactions and not PATHLIKE.search(text)


def build(catalog: dict, goals: dict[str, dict], rng: random.Random) -> list[dict]:
    """Candidate examples: goal, listing, the catalog's label and the split. Held-out routines only ever appear in
    held-out listings, so the held-out set asks about routines the tuned model never saw."""
    by_name = {r["name"]: r for r in catalog["routines"]}
    held = set(catalog["held_out"])
    train_pool = [n for n in by_name if n not in held]
    out: list[dict] = []

    def add(goal: str, source: str, label: str, include: bool) -> None:
        split = "heldout" if source in held else "train"
        pool = [n for n in (by_name if split == "heldout" else train_pool) if n != source]
        names = rng.sample(pool, DISTRACTORS) + ([source] if include else [])
        rng.shuffle(names)
        out.append({"goal": goal, "source": source, "label": label, "split": split,
                    "listing": [entry(by_name[n]) for n in names]})

    for name, g in goals.items():
        for i, goal in enumerate(g.get("same") or []):
            absent = i % ABSENT_EVERY == ABSENT_EVERY - 1
            add(goal, name, "none" if absent else name, include=not absent)
        for goal in g.get("near") or []:
            add(goal, name, "none", include=True)
    for goal, label in STORY:
        add(goal, label if label != "none" else rng.choice(train_pool), label, include=True)
    return out


def chat(client, model: str, msgs: list[dict], schema: dict, name: str, spend: Spend, **kw) -> dict | None:
    spend.check()
    r = client.chat.completions.create(model=models.check_model(model), messages=msgs, response_format={
        "type": "json_schema", "json_schema": {"name": name, "schema": schema}}, **kw)
    spend.add(model, getattr(r, "usage", None))
    try:
        data = json.loads(r.choices[0].message.content or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def generate(client, catalog: dict, spend: Spend, cache: Path) -> dict[str, dict]:
    goals = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    for r in catalog["routines"]:
        if r["name"] in goals:
            continue
        data = chat(client, models.SPARK, [{"role": "system", "content": GEN_RULES},
                                           {"role": "user", "content": json.dumps(entry(r))}],
                    GEN_SCHEMA, "s14_goals", spend, reasoning_effort="low", max_tokens=4000)
        if data:
            goals[r["name"]] = {"same": [str(s).strip() for s in data["same"]][:10],
                                "near": [str(s).strip() for s in data["near"]][:6]}
            cache.write_text(json.dumps(goals, indent=1, ensure_ascii=False), encoding="utf-8")
    return goals


def ask(client, model: str, ex: dict, spend: Spend, **kw) -> str:
    """One routine choice, as Lantern asks it; "?" when the answer is unreadable."""
    data = chat(client, model, messages(ex["goal"], ex["listing"]), MATCH_SCHEMA, "routine_match", spend, **kw)
    return str((data or {}).get("routine", "?")).strip() or "?"


def label(client, examples: list[dict], spend: Spend, cache: Path) -> None:
    done = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    for ex in examples:
        key = ex["goal"] + "\n" + ",".join(e["name"] for e in ex["listing"])
        if key not in done:
            done[key] = ask(client, models.WONDER, ex, spend, reasoning_effort="low", max_tokens=4000)
            cache.write_text(json.dumps(done, indent=1, ensure_ascii=False), encoding="utf-8")
        ex["teacher"] = done[key]


def row(ex: dict) -> dict:
    return {"messages": messages(ex["goal"], ex["listing"]) + [{"role": "assistant", "content": answer(ex["label"])}],
            "expected": ex["label"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="demo/s14/data")
    ap.add_argument("--max-usd", type=float, default=4.0)
    ap.add_argument("--spark-ref", action="store_true", help="also measure Muse Spark on the held-out set")
    args = ap.parse_args(argv)
    if not os.environ.get(models.API_KEY_ENV):
        print(f"set {models.API_KEY_ENV} first")
        return 2
    from openai import OpenAI
    client = OpenAI(base_url=models.BASE_URL, api_key=os.environ[models.API_KEY_ENV], timeout=180)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    spend = Spend(args.max_usd)
    catalog = load_catalog()
    goals = generate(client, catalog, spend, out / "goals.json")
    examples = build(catalog, goals, random.Random(SEED))
    label(client, examples, spend, out / "teacher.json")
    kept = [ex for ex in examples if ex["teacher"] == ex["label"]]
    safe = [ex for ex in kept if clean(json.dumps(row(ex), ensure_ascii=False))]
    report: dict = {"routines": len(catalog["routines"]), "held_out_routines": catalog["held_out"],
                    "candidates": len(examples), "teacher_agreed": len(kept), "dropped_by_scrubber": len(kept) - len(safe),
                    "disagreements": [{"goal": ex["goal"], "label": ex["label"], "teacher": ex["teacher"]}
                                      for ex in examples if ex["teacher"] != ex["label"]]}
    for split in ("train", "heldout"):
        rows = [row(ex) for ex in safe if ex["split"] == split]
        (out / f"{split}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                            encoding="utf-8")
        report[split] = {"examples": len(rows), "none": sum(r["expected"] == "none" for r in rows)}
    if args.spark_ref:
        held = [ex for ex in safe if ex["split"] == "heldout"]
        answers = [ask(client, models.SPARK, ex, spend, reasoning_effort="low", max_tokens=4000) for ex in held]
        report["spark_heldout"] = {"correct": sum(a == ex["label"] for a, ex in zip(answers, held)), "of": len(held),
                                   "model": models.SPARK, "effort": "low"}
    report["cost_usd"], report["calls"] = round(spend.usd, 4), spend.calls
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "disagreements"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
