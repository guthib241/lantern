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
