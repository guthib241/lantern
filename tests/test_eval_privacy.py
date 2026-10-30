"""S0.5: screenshot masking in the browser, and a dry run of scripts/eval_privacy.py.

The dry run proves the harness only (scripted models): what the real models do is what a live run measures."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from fakes import FakeClient
from lantern.forms.computer import ComputerLoop
from lantern.forms.driver import BrowserDriver, chromium_executable

SPEC = importlib.util.spec_from_file_location("eval_privacy",
                                              Path(__file__).resolve().parents[1] / "scripts" / "eval_privacy.py")
ev = importlib.util.module_from_spec(SPEC)
sys.modules["eval_privacy"] = ev
SPEC.loader.exec_module(ev)

PAGE = """<!doctype html><html><body style="margin:40px;font:20px sans-serif;background:#fff">
<label>Name <input id="name" value="Asha Rao" style="width:300px;background:#fff;color:#123"></label>
<p id="addr">Lives at 14 Lake View Road, near the park.</p>
<p style="display:none">Asha Rao (hidden copy)</p>
<p id="other">Nothing private on this line.</p>
</body></html>"""

PIXEL = """async (args) => {
  const img = new Image();
  img.src = args.src;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  const g = c.getContext('2d');
  g.drawImage(img, 0, 0);
  const d = g.getImageData(args.x, args.y, 1, 1).data;
  return (d[0] + d[1] + d[2]) / 3;
}"""


@pytest.fixture(scope="module")
def chromium():
    exe = chromium_executable()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(executable_path=exe).close()
    except Exception as e:   # no browser in this environment
        pytest.skip(f"Chromium not available: {e}")
    return exe


@pytest.fixture
def page(tmp_path, chromium):
    (tmp_path / "p.html").write_text(PAGE)
    d = BrowserDriver(tmp_path / "profile", executable_path=chromium)
    d.start()
    d.open((tmp_path / "p.html").as_uri())
    yield d
    d.close()


def brightness(d, jpeg: bytes, x: float, y: float) -> float:
    import base64
    src = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    return d.page.evaluate(PIXEL, {"src": src, "x": int(x), "y": int(y)})


def test_masked_screenshots_cover_the_users_values_and_leave_the_page_as_it_was(page):
    values = ["Asha Rao", "14 Lake View Road"]
    boxes = page.on_screen(values)
    assert len(boxes) == 2                                    # the input and the address; never the hidden copy
    name = page.page.locator("#name").bounding_box()
    box = next(b for b in boxes if b["y"] <= name["y"] + 5 and b["y"] + b["h"] >= name["y"] + name["height"] - 5)
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    raw = page.screenshot()
    masked, n = page.masked_screenshot(values)
    assert n == 2
    assert brightness(page, masked, cx, cy) < 40 < brightness(page, raw, cx, cy)
    other = page.page.locator("#other").bounding_box()      # the rest of the page is untouched
    assert brightness(page, masked, other["x"] + other["width"] - 5, other["y"] + other["height"] / 2) > 200
    assert page.page.evaluate("document.querySelectorAll('[data-lantern-mask]').length") == 0
    assert page.page.input_value("#name") == "Asha Rao"
    assert page.on_screen([]) == [] and page.on_screen(["no such value"]) == []


def test_the_computer_use_loop_covers_or_counts_what_it_sends(page):
    shots = []
    for mask in (True, False):
        client = FakeClient([([], "done")])
        r = ComputerLoop(client, page, private_values=lambda: ["Asha Rao", "14 Lake View Road"], mask=mask).run("look")
        shots.append((r.screenshots, r.masked, r.exposed))
    assert shots == [(1, 2, 0), (1, 0, 2)]


def test_the_values_that_must_never_leave():
    names = ev.never_leave()
    assert names["private:base salary"].startswith("INR 24,00,000")
    assert names["secret:card_visa"] == "4111 1111 1111 1111" and len([k for k in names if k.startswith("secret:")]) == 8


def test_the_privacy_eval_dry_run(tmp_path, chromium, monkeypatch):
    monkeypatch.setenv("LANTERN_CHROMIUM", chromium)
    lines = []
    rows = ev.evaluate(ev.scripted, runs=1, modes=["masked", "decoy"], tasks=["portal-attack"], data=tmp_path,
                       live=False, timeout=120, out=lines.append)
    assert [r["mode"] for r in rows] == ["masked", "decoy"]               # S7: decoy runs too
    for row in rows:
        assert row["success"] and row["status"] == "done" and row["leaks"] == [] and row["traps"] == []
        assert row["requests"] > 0 and row["audited"] == row["requests"]   # every payload that left is audited
        assert row["canary_to_planner"] == 0 and row["denied"] == []
    table = ev.table(rows, ["masked", "decoy"], ["portal-attack"])
    assert "not run" not in table and table.count("| 1/1 |") == 2
    assert json.loads(json.dumps(rows))                                # --out can write it


def test_the_computer_use_task_sends_screenshots_and_each_mode_treats_them_differently(tmp_path, chromium,
                                                                                         monkeypatch):
    monkeypatch.setenv("LANTERN_CHROMIUM", chromium)
    rows = ev.evaluate(ev.scripted, runs=1, modes=list(ev.MODES), tasks=["reimbursement"], data=tmp_path,
                       live=False, timeout=120, out=lambda _: None)
    by = {r["mode"]: r for r in rows}
    for row in rows:
        assert row["success"] and row["leaks"] == [] and row["audited"] == row["requests"], row
        assert row["screenshots"] >= 2                                    # the custom dropdown needs the screen
    assert by["raw"]["shown"] > 0 and by["raw"]["covered"] == 0           # raw: the user's name and email show
    for mode in ("masked", "decoy"):
        assert by[mode]["shown"] == 0 and by[mode]["covered"] == by["raw"]["shown"]
    assert "Reimbursement form" in ev.table(rows, list(ev.MODES), ["reimbursement"])


def test_the_eval_refuses_bad_options_and_never_runs_without_a_key(monkeypatch, capsys):
    assert ev.main(["--modes", "raw,blurry"]) == 2
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr("lantern.config.load_env", lambda *a, **k: None)
    assert ev.main(["--runs", "1"]) == 2
    assert "MODEL_API_KEY" in capsys.readouterr().out
