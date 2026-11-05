"""Live, Oct 28: on demo/portal every fill_form call after the first landed back on page 1, because each call opens
a fresh browser and the portal's session cookie was lost. A task's cookies now carry over (memory only). Also the
"visual" fill mode, which hands every field but uploads to the computer-use model. No browser, no model."""
from types import SimpleNamespace as NS

from lantern.forms.filler import FormService, FormSettings, _Run


class Context:
    def __init__(self, jar=None):
        self.jar, self.added = list(jar or []), []

    def cookies(self):
        return list(self.jar)

    def add_cookies(self, jar):
        self.added.extend(jar)


def service(tmp_path, **kw):
    return FormService(FormSettings(media_dir=tmp_path, vault_path=tmp_path / "p.json", **kw), None, None, None,
                       driver_factory=lambda: None)


def test_a_tasks_cookies_carry_to_its_next_fill_and_no_other_task_gets_them(tmp_path):
    svc = service(tmp_path)
    first = NS(page=NS(context=Context([{"name": "cpp_session", "value": "abc", "domain": "127.0.0.1", "path": "/"}])))
    svc._save_session(first, 7)
    again, other = NS(page=NS(context=Context())), NS(page=NS(context=Context()))
    svc._load_session(again, 7)
    svc._load_session(other, 8)
    assert [c["value"] for c in again.page.context.added] == ["abc"] and other.page.context.added == []
    svc._save_session(first, None)                       # a fill outside a task keeps nothing
    assert set(svc._cookies) == {7}


def test_visual_mode_hands_what_a_screenshot_shows_to_computer_use(tmp_path):
    """Uploads, dropdowns and date pickers stay in code: their popups never show in a screenshot (live, Oct 28)."""
    fields = [{"id": "f1", "label": "Full name", "name": "full_name", "html_id": "full_name", "type": "text"},
              {"id": "f2", "label": "Site plan", "name": "plan", "html_id": "plan", "type": "file"},
              {"id": "f3", "label": "Permit type", "name": "permit_type", "html_id": "permit_type", "type": "select"},
              {"id": "f4", "label": "Start date", "name": "start", "html_id": "start", "type": "date"}]
    values = [{"field": "Full name", "value": "Asha Rao"}, {"field": "Site plan", "value": str(tmp_path / "x.pdf")},
              {"field": "Permit type", "value": "Home renovation"}, {"field": "Start date", "value": "2026-11-20"}]
    for mode, expect_visual in (("dom", []), ("visual", ["Full name"])):
        svc = service(tmp_path, fill=mode)
        svc._upload = lambda d, f, path: (True, "")
        d = NS(set_value=lambda f, v: True)
        run = _Run()
        svc._apply(d, fields, values, run)
        assert [f["label"] for f, _ in run.visual] == expect_visual
        assert {"Site plan", "Permit type", "Start date"} <= {x["field"] for x in run.filled}   # in code either way
