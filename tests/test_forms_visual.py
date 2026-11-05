"""Ported from claude/real-run (live, Oct 28): visual fill mode, and the computer-use loop surviving a lost chain.
Scripted model, fake screen; no browser, no API."""
from types import SimpleNamespace as NS

from fakes import FakeClient, computer_call
from lantern.forms.computer import ComputerLoop
from lantern.forms.filler import OFF_SCREEN, FormSettings, on_screen

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class Screen:
    width, height, url = 1280, 800, "http://127.0.0.1:8770/apply"

    def __init__(self):
        self.shots = 0

    def screenshot(self):
        self.shots += 1
        return PNG

    def blocked_submits(self):
        return 0

    def wait(self, ms):
        pass

    def settle(self):
        pass


class Lost(Exception):
    def __init__(self):
        super().__init__("referenced response not found or expired")
        self.status_code, self.body = 400, {"error": {"message": str(self)}}
        self.response = NS(status_code=400, headers={})


class DropsTheChain(FakeClient):
    def create(self, **kw):
        if kw.get("previous_response_id") and not getattr(self, "dropped", False):
            self.dropped = True
            self.requests.append(kw)
            raise Lost()
        return super().create(**kw)


def test_a_lost_chain_mid_form_continues_from_a_fresh_screenshot():
    screen = Screen()
    client = DropsTheChain([([computer_call("c1", {"type": "wait", "ms": 10})], ""), ([], "Filled.")])
    r = ComputerLoop(client, screen).run("Fill the permit form.")
    assert r.status == "done" and r.message == "Filled."
    retry = client.requests[2]
    assert "previous_response_id" not in retry
    text, image = retry["input"][0]["content"]
    assert text["text"].startswith("Fill the permit form.") and "not available" in text["text"]
    assert image["type"] == "input_image" and image["image_url"].startswith("data:image/")


def test_other_errors_still_fail_the_fill():
    class Broken(FakeClient):
        def create(self, **kw):
            if kw.get("previous_response_id"):
                raise RuntimeError("500 server error")
            return super().create(**kw)
    client = Broken([([computer_call("c1", {"type": "wait", "ms": 10})], "")])
    try:
        ComputerLoop(client, Screen()).run("Fill it.")
    except RuntimeError as e:
        assert "500" in str(e)
    else:
        raise AssertionError("a server error must not be treated as a lost chain")


def test_visual_mode_leaves_only_off_screen_popups_to_the_page(monkeypatch):
    assert not on_screen("dom", "text")
    assert on_screen("visual", "text") and on_screen("visual", "checkbox")
    assert not any(on_screen("visual", t) for t in OFF_SCREEN)   # Wonder can't see native select/date popups
    cfg = NS(forms_model="muse-wonder-1.0", model="muse-spark-1.3", forms_protocol="auto", forms_coords="",
             forms_headless=True, chromium_path="", forms_domains=[], browser_profile_dir="/tmp/p", media_dir="/tmp/m",
             profile_path="/tmp/profile.json", forms_screenshots="masked")
    monkeypatch.delenv("LANTERN_FORMS_FILL", raising=False)
    assert FormSettings.from_config(cfg).fill == "dom"
    monkeypatch.setenv("LANTERN_FORMS_FILL", "Visual")
    assert FormSettings.from_config(cfg).fill == "visual"
