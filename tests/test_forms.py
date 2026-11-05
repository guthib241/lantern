"""M7 (no browser): hard-stop rules, profile vault, action parsing, URL allowlist, scrubber vs screenshots."""
import base64
import os
import stat

import pytest

from lantern.forms import guards
from lantern.forms.computer import ActionError, call_actions, parse_action
from lantern.forms.driver import iso_date, playwright_key
from lantern.forms.filler import FormError, FormSettings, resolve_url, submit_payload, url_problem
from lantern.forms.vault import Vault, VaultError
from lantern.scrubber import DEFAULT as SCRUBBER
from fakes import computer_call


# ---- hard stops ---------------------------------------------------------------------
@pytest.mark.parametrize("field, reason", [
    ({"type": "password", "label": "Your secret"}, "password field"),
    ({"type": "text", "autocomplete": "one-time-code", "label": "Code"}, "one-time code"),
    ({"type": "text", "autocomplete": "cc-number", "label": "Number"}, "payment card field"),
    ({"type": "text", "autocomplete": "new-password"}, "password field"),
    ({"type": "text", "label": "OTP"}, "one-time code"),
    ({"type": "text", "label": "Enter the verification code"}, "one-time code"),
    ({"type": "text", "label": "CVV"}, "payment card field"),
    ({"type": "text", "name": "cardNumber"}, "payment card field"),
    ({"type": "text", "label": "UPI PIN"}, "payment card field"),
    ({"type": "text", "label": "Aadhaar number"}, "government ID number"),
    ({"type": "text", "name": "panNumber"}, "government ID number"),
    ({"type": "text", "html_id": "pan"}, "government ID number"),
    ({"type": "text", "label": "Passport no."}, "government ID number"),
    ({"type": "text", "label": "SSN"}, "government ID number"),
    ({"type": "frame", "frame": "https://js.stripe.com/v3/elements"}, "payment frame"),
    ({"type": "frame", "frame": "https://www.google.com/recaptcha/api2/anchor"}, "CAPTCHA"),
    ({"type": "checkbox", "label": "I'm not a robot", "captcha": True}, "CAPTCHA"),
])
def test_sensitive_fields_are_left_to_the_user(field, reason):
    assert guards.classify(field) == reason


@pytest.mark.parametrize("label", ["Full name", "Email", "Company", "Expense date", "PIN code", "Pincode",
                                   "Amount", "Japan office", "Team name", "Purpose"])
def test_ordinary_fields_are_fillable(label):
    assert guards.classify({"type": "text", "label": label}) is None


def test_values_that_look_like_cards_or_ids_are_refused():
    assert "card" in guards.value_reason("4111 1111 1111 1111")
    assert "PAN" in guards.value_reason("ABCPE1234F")
    assert guards.value_reason("2400") is None and guards.value_reason("Taxi to the venue") is None


def test_normalize_splits_camel_case_and_separators():
    assert guards.normalize("userPanNumber") == "user pan number"
    assert guards.normalize("card_number[0]") == "card number 0"


# ---- vault ----------------------------------------------------------------------------
def test_vault_round_trip_and_private_file(tmp_path):
    v = Vault(tmp_path / "profile.json")
    v.set("full name", "Asha Rao")
    v.set("why join?", "To build things", section="answers")
    v.set("hackathon reimbursement", "https://forms.example.org/claim", section="forms")
    assert v.for_model() == {"profile": {"full name": "Asha Rao"}, "answers": {"why join?": "To build things"}}
    assert v.bookmarks() == {"hackathon reimbursement": "https://forms.example.org/claim"}
    assert stat.S_IMODE(os.stat(tmp_path / "profile.json").st_mode) == 0o600
    assert v.remove("full name") and not v.remove("full name")


@pytest.mark.parametrize("key, value", [
    ("password", "hunter2"), ("card number", "4111"), ("aadhaar", "1234"), ("otp", "123456"),
    ("note", "4111 1111 1111 1111"), ("tax", "ABCPE1234F"), ("token", "ghp_" + "a" * 36),
])
def test_vault_never_stores_secrets(tmp_path, key, value):
    with pytest.raises(VaultError):
        Vault(tmp_path / "p.json").set(key, value)
    assert not (tmp_path / "p.json").exists()


# ---- actions from either harness ------------------------------------------------------
def test_parse_actions_from_both_harnesses():
    assert parse_action({"type": "click", "button": "left", "x": 412, "y": 315}) == \
        {"kind": "click", "x": 412.0, "y": 315.0, "button": "left", "count": 1}
    assert parse_action({"action": "left_click", "coordinate": [300, 200]})["x"] == 300.0
    assert parse_action({"type": "double_click", "x": 1, "y": 2})["count"] == 2
    assert parse_action({"action": "right_click", "coordinate": [1, 2]})["button"] == "right"
    assert parse_action({"type": "keypress", "keys": ["CTRL", "A"]})["key"] == "Control+a"
    assert parse_action({"action": "key", "text": "Return"})["key"] == "Enter"
    assert parse_action({"type": "type", "text": "hello"}) == {"kind": "type", "text": "hello"}
    s = parse_action({"type": "scroll", "x": 10, "y": 20, "scroll_x": 0, "scroll_y": 300})
    assert (s["dx"], s["dy"]) == (0, 300)
    s = parse_action({"action": "scroll", "coordinate": [500, 500], "scroll_direction": "up", "scroll_amount": 2})
    assert s["dy"] == -200
    assert parse_action({"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]})["path"] == [(1.0, 2.0), (3.0, 4.0)]
    assert parse_action({"action": "left_click_drag", "start_coordinate": [1, 2], "coordinate": [3, 4]})["kind"] == "drag"
    assert parse_action({"type": "wait"})["ms"] == 1000
    assert parse_action({"action": "wait", "duration": 60})["ms"] == 5000
    for bad in ({"type": "click"}, {"type": "teleport"}, {"type": "type"}, "nope"):
        with pytest.raises(ActionError):
            parse_action(bad)


def test_computer_call_with_single_action_or_list():
    from types import SimpleNamespace as NS
    assert len(call_actions(computer_call("c", {"type": "click", "x": 1, "y": 1}, {"type": "type", "text": "x"}))) == 2
    assert call_actions(NS(type="computer_call", call_id="c", action={"type": "screenshot"}))[0]["kind"] == "screenshot"


def test_playwright_key_names():
    assert playwright_key("ctrl+shift+a") == "Control+Shift+A"
    assert playwright_key(["ESC"]) == "Escape"
    assert playwright_key("pagedown") == "PageDown"
    assert playwright_key(["f5"]) == "F5"


def test_iso_date():
    assert iso_date("25/10/2026") == "2026-10-25"
    assert iso_date("Oct 25, 2026") == "2026-10-25"
    assert iso_date("2026-10-25") == "2026-10-25"


# ---- URLs -------------------------------------------------------------------------------
def test_url_allowlist(tmp_path):
    s = FormSettings(demo_dir=tmp_path, domains=("forms.example.org",))
    assert url_problem((tmp_path / "a.html").as_uri(), s) is None
    assert url_problem("file:///etc/passwd", s)
    assert url_problem("http://127.0.0.1:8765/demo", s) is None
    assert url_problem("https://forms.example.org/x", s) is None
    assert url_problem("https://sub.forms.example.org/x", s) is None
    assert url_problem("https://evil-forms.example.org.attacker.test/", s)
    assert url_problem("https://example.com", s)
    assert url_problem("javascript:alert(1)", s)


def test_resolve_form_names(tmp_path):
    s = FormSettings(demo_dir=tmp_path)
    v = Vault(tmp_path / "p.json")
    assert resolve_url("hackathon reimbursement form", s, v) == (tmp_path / "reimbursement.html").as_uri()
    assert resolve_url("https://x.test/f", s, v) == "https://x.test/f"
    v.set("expense claim", "https://forms.example.org/claim", section="forms")
    assert resolve_url("Expense claim", s, v) == "https://forms.example.org/claim"
    with pytest.raises(FormError):
        resolve_url("tax return", s, v)


def test_submit_payload_holds_hashes_not_values():
    p = submit_payload("file:///f.html", "Submit", [{"id": "f1", "label": "Name", "value": "Asha Rao"}], "a=1")
    assert "Asha Rao" not in str(p) and len(p["fields"][0][1]) == 64
    changed = submit_payload("file:///f.html", "Submit", [{"id": "f1", "label": "Name", "value": "Mallory"}], "a=1")
    assert p != changed


# ---- scrubber leaves screenshots alone ---------------------------------------------------
def test_scrubber_skips_base64_screenshots_but_scrubs_text():
    # base64 that happens to contain a PAN-shaped run between '/' characters
    image = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" * 30).decode() + "/ABCPE1234F/AAAA"
    payload = [{"type": "input_image", "image_url": image},
               {"type": "input_text", "text": "my PAN is ABCPE1234F"}]
    out, found = SCRUBBER.scrub_obj(payload)
    assert out[0]["image_url"] == image
    assert "ABCPE1234F" not in out[1]["text"] and found["pan"] == 1
