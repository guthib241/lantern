"""S7 Decoy Screens. Stand-ins keep a value's shape and are stable within a task; in Chromium the screenshot that
leaves shows them in the page's own elements (pixel for pixel what the page looks like holding the stand-ins), and
the page is exactly as it was afterwards; the computer-use model (scripted) types a stand-in and the real value is
what lands in the field; typing a real value, or anything but the stand-in into a guarded field, is refused."""
import io
import json

import pytest

from fakes import FakeClient, call, click, computer_call, usage
from lantern.decoy import Decoys, DecoyError, stand_in
from lantern.forms.computer import ComputerLoop
from lantern.forms.driver import BrowserDriver, DriverError
import test_eval_privacy
import test_forms_browser
from lantern.forms.vault import Vault

chromium = test_eval_privacy.chromium   # the same fixtures
at = test_forms_browser.at

NAME, ROAD, PHONE = "Asha Rao", "14 Lake View Road", "+91 98450 12345"
PAGE = """<!doctype html><html><body style="margin:40px;font:20px sans-serif;background:#fff">
<label for="name">Full name</label> <input id="name" style="width:300px"><br><br>
<label for="city">City</label> <input id="city" style="width:300px"><br><br>
<label for="saved">Saved address</label> <input id="saved" value="14 Lake View Road" style="width:300px">
<p id="addr">Lives at 14 Lake View Road, near the park. Call +91 98450 12345.</p>
<p id="other">Nothing private on this line.</p>
</body></html>"""


# ---- the stand-ins --------------------------------------------------------------------------------------
def test_a_stand_in_keeps_the_shape_and_is_never_the_value():
    key = b"k" * 32
    for value in (NAME, ROAD, PHONE, "AB12 CD", "x9"):
        f = stand_in(key, value)
        assert len(f) == len(value) and f != value
        for a, b in zip(value, f):
            assert (a.isdigit(), a.isupper(), a.islower()) == (b.isdigit(), b.isupper(), b.islower())
            assert a == b if not a.isalnum() else a != b      # separators kept, every letter and digit changed
    assert stand_in(key, NAME) == stand_in(key, NAME) != stand_in(b"j" * 32, NAME)


def test_the_map_hides_reveals_and_refuses():
    d = Decoys([NAME, ROAD, PHONE, "ok"], key=b"k" * 32)
    assert set(d.fake) == {NAME, ROAD, PHONE}                       # too short to matter: left alone
    assert all(r.lower() not in f.lower() for f in d.fake.values() for r in d.fake)
    text = f"Name: {NAME.upper()}, lives at {ROAD}"
    hidden = d.hide(text)
    assert NAME.lower() not in hidden.lower() and ROAD not in hidden and d.fake[ROAD] in hidden
    told = d.guard("Full name", NAME)
    assert told == d.fake[NAME] and d.guard("City", "Pune") == "Pune"   # not a private value: as it is
    assert d.reveal(told, "Full name") == NAME and d.swapped == 1
    assert d.reveal(f"near {d.fake[ROAD]}") == f"near {ROAD}"
    assert d.reveal("Pune", "City") == "Pune"
    with pytest.raises(DecoyError, match="never shown"):
        d.reveal("asha rao", "City")
    with pytest.raises(DecoyError, match="only the stand-in"):
        d.reveal("Someone Else", "full name")


# ---- the screenshot -------------------------------------------------------------------------------------
@pytest.fixture
def page(tmp_path, chromium):
    (tmp_path / "p.html").write_text(PAGE)
    d = BrowserDriver(tmp_path / "profile", executable_path=chromium)
    d.start()
    d.open((tmp_path / "p.html").as_uri())
    yield d
    d.close()


def pixels(jpeg: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(jpeg)).convert("L")


def test_the_screenshot_shows_stand_ins_in_the_pages_own_elements_and_puts_everything_back(page):
    d = Decoys([NAME, ROAD, PHONE])
    page.page.fill("#name", NAME)
    page.page.focus("#name")
    page.page.evaluate("document.getElementById('name').setSelectionRange(2, 5)")
    before = page.page.evaluate("[document.getElementById('addr').textContent, document.getElementById('saved').value]")
    sent, n = page.decoy_screenshot(d.pairs())
    assert n == 4                                                   # name and saved fields, road and phone in text
    after = page.page.evaluate("[document.getElementById('addr').textContent, document.getElementById('saved').value,"
                               " document.getElementById('name').value, document.activeElement.id,"
                               " document.getElementById('name').selectionStart,"
                               " document.getElementById('name').selectionEnd]")
    assert after[:2] == before and after[2:] == [NAME, "name", 2, 5]   # exactly as it was, caret and all
    page.page.evaluate("document.activeElement.blur()")
    sent, _ = page.decoy_screenshot(d.pairs())
    # What left is what the page looks like holding the stand-ins: same font, size and place.
    page.page.evaluate("(p) => { const sw = (t) => p.reduce((s, [r, f]) => s.split(r).join(f), t);"
                       " for (const id of ['name', 'saved']) { const el = document.getElementById(id); el.value = sw(el.value); }"
                       " const a = document.getElementById('addr'); a.textContent = sw(a.textContent); }", d.pairs())
    reference = page.screenshot()
    from PIL import ImageChops
    diff = ImageChops.difference(pixels(sent), pixels(reference))
    assert diff.getextrema()[1] <= 24                                # JPEG noise only
    raw = page.page.evaluate("document.getElementById('name').value")
    assert raw == d.fake[NAME]


def test_a_page_that_keeps_the_stand_in_stops_the_run(page):
    d = Decoys([NAME])
    page.page.fill("#name", NAME)
    page.page.evaluate("""() => {
      const el = document.getElementById('name');
      const real = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
      let writes = 0;   // accepts the stand-in, ignores Lantern putting the real value back
      Object.defineProperty(el, 'value', {configurable: true, get() { return real.get.call(this); },
                                          set(v) { if (writes++ === 0) real.set.call(this, v); }});
    }""")
    with pytest.raises(DriverError, match="stand-in"):
        page.decoy_screenshot(d.pairs())


# ---- the loop: stand-ins typed, real values land ----------------------------------------------------------
def centre(d, sel, normalized=True):
    b = d.page.locator(sel).bounding_box()
    x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    return [round(x * 1000 / 1280), round(y * 1000 / 800)] if normalized else (x, y)


def run(page, script, decoys, shots=None):
    loop = ComputerLoop(FakeClient(script), page, protocol="function", decoys=decoys,
                        on_decoy=(lambda seen, sent, n: shots.append((seen, sent, n))) if shots is not None else None)
    return loop, loop.run("finish the form")


def test_the_model_types_the_stand_in_and_the_real_value_lands(page):
    d = Decoys([NAME, ROAD])
    told = d.guard("Full name", NAME)
    shots = []
    script = [([call("computer.computer", "f1", action="left_click", coordinate=centre(page, "#name"))], ""),
              ([call("computer.computer", "f2", action="type", text=told)], ""),
              ([call("computer.computer", "f3", action="left_click", coordinate=centre(page, "#city"))], ""),
              ([call("computer.computer", "f4", action="type", text="Pune")], ""),
              ([call("computer.stop", "f5", answer=f"Filled the name ({told}) and the city.")], "")]
    loop, r = run(page, script, d, shots)
    assert r.status == "done" and r.swapped == 1
    assert page.page.input_value("#name") == NAME and page.page.input_value("#city") == "Pune"
    sent_text = json.dumps([q.get("input") for q in loop.client.requests], default=str)
    assert NAME not in sent_text and ROAD not in sent_text           # no real value in anything that left
    assert r.actions[1] == f"type {len(told)} characters"            # what goes back says nothing more
    assert len(shots) == r.screenshots and shots[-1][2] == 3          # the name, the saved road, the road in text
    assert all(seen != sent for seen, sent, n in shots if n)


@pytest.mark.parametrize("typed, why", [(NAME, "never shown"), ("Someone Else", "only the stand-in")])
def test_typing_a_real_value_or_anything_else_into_a_guarded_field_is_refused(page, typed, why):
    d = Decoys([NAME])
    d.guard("Full name", NAME)
    script = [([computer_call("c1", click(*centre(page, "#name", normalized=False)), {"type": "type", "text": typed})], "")]
    loop = ComputerLoop(FakeClient(script), page, decoys=d)
    r = loop.run("fill it")
    assert r.status == "hard_stop" and why in r.message and page.page.input_value("#name") == ""


def test_fill_form_in_decoy_mode_keeps_the_pair_and_reports_it(tmp_path, chromium, at):
    """The demo reimbursement form: Lantern fills the name itself; the computer-use model picks the category on a
    screen where the name shows as its stand-in; the desktop gets the side-by-side pair."""
    combo, travel = at["reimbursement.html"]["#category"], at["reimbursement.html"]['li[data-value="travel"]']
    root = tmp_path / "docs"
    root.mkdir()
    Vault(tmp_path / "profile.json").set("full name", NAME)
    client = FakeClient([
        ([call("set_values", "p1", values=[{"field": "Full name", "value": NAME},
                                           {"field": "Expense category", "value": "Travel"}])], "", usage(900, 0, 80)),
        ([computer_call("c1", click(*combo))], "", usage(2000, 0, 30)),
        ([computer_call("c2", click(*travel))], "", usage(3000, 1500, 30)),
        ([], "Picked Travel.", usage(4000, 3000, 10)),
    ])
    tb, bus, asked, mem = test_forms_browser.toolbox(tmp_path, client, chromium, root=root, screenshots="decoy")
    events = []
    tb.on_event = lambda kind, data: events.append((kind, data))
    r = tb.fill_form("hackathon reimbursement", "Claim my taxi")
    assert "still empty: Email" in r["reason"]                       # only two values given: the rest stays empty
    assert r["screen_privacy"]["mode"] == "decoy" and r["screen_privacy"]["masked"] >= 3   # the name, each screenshot
    assert r["screen_privacy"]["shown"] == 0
    assert NAME not in json.dumps([q.get("input") for q in client.requests[1:]], default=str)
    pairs = [e for k, e in events if k == "decoy"]
    assert len(pairs) == r["screenshots"] == 3
    media = tmp_path / "media"
    assert all((media / e["seen"]).exists() and (media / e["sent"]).exists() for e in pairs)
    forms = tb.forms
    for i in range(8):                                                # only the last few pairs are kept
        forms._decoy_pair(b"seen", b"sent", 0, lambda kind, data: None)
    assert len(list(media.glob("decoy_*-sent.jpg"))) == len(list(media.glob("decoy_*-seen.jpg"))) == 6
