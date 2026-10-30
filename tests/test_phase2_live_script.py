"""A dry run of scripts/phase2_live.py with a scripted model, so a real run with the key doesn't spend money
finding bugs in the script itself. This proves the plumbing only; what the real models do is what the live
run measures."""
import importlib.util
import json
import sys
import time
from pathlib import Path

from fakes import call
from lantern.demo import ORGANISER
from test_scheduler import Routed, finish

SPEC = importlib.util.spec_from_file_location("phase2_live", Path(__file__).resolve().parents[1] / "scripts" / "phase2_live.py")
live = importlib.util.module_from_spec(SPEC)
sys.modules["phase2_live"] = live   # dataclasses look their module up while the class is made
SPEC.loader.exec_module(live)


class Slow(Routed):
    """Real calls take seconds; a little delay lets the script's mid-task hooks act, as they would live."""

    def create(self, **kw):
        time.sleep(0.25)
        return super().create(**kw)


def scripts():
    mail = dict(to=[ORGANISER], subject="Lantern live check")
    return {
        "permit type": [([call("ask_user", "a1", question="Residential or commercial?",
                               options=["residential", "commercial"])], ""),
                        finish("a2", "commercial"), ([], "Commercial.")],
        "privacy zone": [([call("list_folders", "b1")], ""), finish("b2", "listed"), ([], "Listed.")],
        "Tidy my Downloads": [([call("organize_files", "c1", moves=[
            {"source": "Downloads/IMG_2041.jpg", "destination": "Downloads/Images/"}])], ""),
            finish("c2", "tidied"), ([], "Tidied.")],
        "no-such-file": [([call("read_file", "d1", path="Documents/no-such-file.txt")], ""),
                         ([call("list_dir", "d2", path="Documents")], ""), finish("d3", "listed"), ([], "Listed.")],
        "Lantern live check": [([call("send_email", "e1", body="test 1", **mail)], ""),
                               ([call("send_email", "e2", body="test 2", **mail)], ""),
                               finish("e3", "sent once"), ([], "Sent once.")],
        "which folder has more files": [([call("list_dir", "f1", path="Downloads")], ""),
                                        ([call("list_dir", "f2", path="Documents")], ""),
                                        finish("f3", "Downloads"), ([], "Downloads.")],
        "summarise both in one line": [([call("list_folders", "g1")], ""), ([call("list_dir", "g2", path="Documents")], ""),
                                       finish("g3", "summarised"), ([], "Summarised.")],
    }


def test_every_check_runs_end_to_end_on_a_scripted_model(tmp_path, monkeypatch):
    monkeypatch.setenv("LANTERN_KEY_STORE", "file")
    monkeypatch.setenv("LANTERN_TASK_BUDGET_USD", live.TASK_LIMIT)   # run() would set it for the whole process
    client = live.Recorder(Slow(scripts()))
    lines = []
    report = live.run(client, tmp_path, [1, 2, 3, 4, 5], timeout=30, out=lines.append)
    assert [r["n"] for r in report] == [1, 2, 3, 4, 5]
    failed = {r["title"]: r["notes"] for r in report if not r["ok"]}
    assert not failed, json.dumps(failed, indent=1)
    assert any("emails in the outbox: 1" in n for n in report[2]["notes"])
    assert any("Wonder started a fresh chain with a recap: True" in n for n in report[3]["notes"])
    assert lines[-1].startswith("Total measured: $0.0000 · 5/5 passed")


class PortalAgent:
    """A scripted agent for check 6: reads the portal's pages from the goal and fills them one by one; answers
    the form filler's planning calls from the seeded applicant (by field label)."""

    def __init__(self, labels: dict):
        self.labels, self.chains, self.n = labels, {}, 0
        self.responses = self

    def create(self, **kw):
        import re as _re
        from types import SimpleNamespace as NS
        self.n += 1
        rid = f"pa_{self.n}"
        text = str(kw.get("input"))
        if "Form fields:" in text:   # the form filler's planning call
            fields = json.loads(text.split("Form fields:\n", 1)[1].split("\n\nUser profile:", 1)[0])
            vals = [{"field": f["id"], "value": self.labels[f["label"]]} for f in fields if f["label"] in self.labels]
            return NS(id=rid, output=[call("set_values", "sv", values=vals)], output_text="", usage=None)
        prev = kw.get("previous_response_id")
        pages = self.chains.pop(prev, None) if prev else _re.findall(r"http://127\.0\.0\.1:\d+/apply/\w+", text)
        if pages is None:
            return NS(id=rid, output=[], output_text="Filed.", usage=None)
        if pages:
            self.chains[rid] = pages[1:]
            return NS(id=rid, output=[call("fill_form", f"c{self.n}", url=pages[0], goal="this page")],
                      output_text="", usage=None)
        self.chains[rid] = None
        return NS(id=rid, output=[call("finish", "f", status="done", summary="filed the permit application")],
                  output_text="", usage=None)


def test_check_6_learns_heals_and_runs_at_zero_on_a_scripted_model(tmp_path, monkeypatch):
    pytest = __import__("pytest")
    from lantern.forms.driver import chromium_executable
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(executable_path=chromium_executable()).close()
    except Exception as e:
        pytest.skip(f"Chromium not available: {e}")
    from test_routines import Both, Chat, code_answer, contract_answer
    sys.path.insert(0, str(live.PORTAL))
    try:
        import portal as pm
    finally:
        sys.path.remove(str(live.PORTAL))
    seed = json.loads((live.PORTAL / "seed.json").read_text(encoding="utf-8"))["applicant"]
    v1 = {pm.LABELS["v1"][k][1]: seed.get(k) for k in pm.LABELS["v1"] if seed.get(k)}
    v1.update({"Permit type": "Home renovation", "ID proof you will bring": "Passport", "How will you pay?": "counter",
               "I declare that the information I gave is true.": "yes"})
    v1_to_v2 = {pm.LABELS["v1"][k][1].lower(): pm.LABELS["v2"][k][1] for k in pm.LABELS["v1"]}
    v1_to_v2.update({pm.BUTTONS["v1"][k][1].lower(): pm.BUTTONS["v2"][k][1] for k in pm.BUTTONS["v1"]})

    def repair(kw):
        q = json.loads(kw["messages"][1]["content"])
        listed = json.loads(next(v for k, v in q.items() if k.startswith("page_"))["content"])["candidates"]
        want = v1_to_v2.get(q["used_to_be"].lower(), "").lower()
        return {"target": next((c["label"] for c in listed if c["label"].lower() == want), "none")}
    agent = PortalAgent(v1)
    real_run = live.check_routines

    def with_plan_file(cfg, client, timeout):   # the scripted planner attaches the plan from the rehearsal folder
        v1["Site plan (PDF, PNG or JPG, up to 2 MB)"] = str(Path(cfg.roots[0]) / "Documents" / "site_plan.pdf")
        return real_run(cfg, client, timeout)
    monkeypatch.setattr(live, "CHECKS", live.CHECKS[:5] + [(live.CHECKS[5][0], with_plan_file)])
    monkeypatch.setenv("LANTERN_KEY_STORE", "file")
    monkeypatch.setenv("LANTERN_TASK_BUDGET_USD", live.TASK_LIMIT)
    monkeypatch.setenv("LANTERN_FORMS_HEADLESS", "1")
    client = live.Recorder(Both(agent, Chat(routine_contract=contract_answer, routine_code=code_answer,
                                            routine_repair=repair)))
    lines = []
    [r] = live.run(client, tmp_path, [6], timeout=90, out=lines.append)
    assert r["ok"], "\n".join(lines)
    assert any("2nd run (routine, v1 layout): done, 0 model calls" in n for n in r["notes"])
    assert any("4th run (healed routine): done, 0 model calls" in n for n in r["notes"])
