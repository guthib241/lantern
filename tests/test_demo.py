"""M17: rehearsal mode. The scripted model plays the 3-minute story; everything else is real."""
import json
from pathlib import Path

import pytest

from lantern import cli, demo, models
from lantern.forms.driver import chromium_executable

PLANNER_INPUT = """Goal:
Claim my taxi to the hackathon venue: INR 2400 on 25 Oct 2026, category Travel, purpose 'Taxi to the venue'. Receipt file: /tmp/My Docs/taxi_receipt.txt

Form fields:
[{"id": "f1", "label": "Full name", "type": "text", "required": true}, {"id": "f2", "label": "Email", "type": "email", "required": true}, {"id": "f3", "label": "Phone", "type": "tel", "required": false}, {"id": "f4", "label": "Expense category", "type": "combobox", "required": true}, {"id": "f5", "label": "Expense date", "type": "date", "required": true}, {"id": "f6", "label": "Amount", "type": "number", "required": true}, {"id": "f7", "label": "Currency", "type": "select", "required": false}, {"id": "f8", "label": "Receipt (PDF or image)", "type": "file", "required": false}, {"id": "f9", "label": "Purpose", "type": "textarea", "required": true}, {"id": "f10", "label": "I confirm these expenses are accurate and were for the hackathon.", "type": "checkbox", "required": true}]

User profile:
{"profile": {"full name": "Asha Rao", "email": "asha@example.com", "phone": "[REDACTED:phone]"}, "answers": {}}

Other things the user told Lantern:
{}"""


def test_planner_half_maps_goal_and_profile_and_skips_redactions():
    values = {v["field"]: v["value"] for v in demo.plan_values(PLANNER_INPUT)}
    assert values == {
        "Full name": "Asha Rao", "Email": "asha@example.com", "Expense category": "Travel",
        "Expense date": "2026-10-25", "Amount": "2400", "Currency": "INR",
        "Receipt (PDF or image)": "/tmp/My Docs/taxi_receipt.txt", "Purpose": "Taxi to the venue",
        "I confirm these expenses are accurate and were for the hackathon.": "yes"}


def test_model_recognises_callers_and_chains_by_response_id():
    m = demo.DemoModel(locate=lambda: {"category": (10, 20), "travel": (10, 60)})
    agent_tools = [{"type": "function", "name": "which_projects_use"}, {"type": "function", "name": "finish"}]
    r1 = m.create(model=models.SPARK, input="Which projects use numpy?", tools=agent_tools)
    fc = r1.output[0]
    assert fc.name == "which_projects_use" and json.loads(fc.arguments) == {"package": "numpy"}
    assert r1.usage.input_tokens == 0 and r1.usage.output_tokens == 0
    out = {"type": "function_call_output", "call_id": fc.call_id,
           "output": json.dumps({"projects": [{"project": "vision-lab", "installed": {"venv": "2.1.3"}}],
                                 "scanned_projects": 4})}
    r2 = m.create(model=models.SPARK, previous_response_id=r1.id, input=[out], tools=agent_tools)
    assert r2.output == [] and "vision-lab (installed 2.1.3)" in r2.output_text
    # an unknown goal gets the rehearsal's help, never an invented answer
    assert "rehearsal mode" in m.create(model=models.SPARK, input="book me a flight", tools=agent_tools).output_text
    # computer use: click the dropdown, click Travel, then report
    c1 = m.create(model=models.WONDER, input=[], tools=[{"type": "computer"}])
    c2 = m.create(model=models.WONDER, previous_response_id=c1.id, input=[], tools=[{"type": "computer"}])
    c3 = m.create(model=models.WONDER, previous_response_id=c2.id, input=[], tools=[{"type": "computer"}])
    assert [c.output[0].actions[0]["y"] for c in (c1, c2)] == [20, 60] and "Travel" in c3.output_text
    with pytest.raises(demo.DemoError):
        m.create(model=models.SAM, input="x", stream=True)
    r = m.create(model=models.SPARK, input="Remind me in 20 minutes to record the demo video.", tools=agent_tools)
    assert json.loads(r.output[0].arguments) == {"text": "record the demo video", "when": "in 20 minutes"}


def test_cli_demo_run_wiring(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo, "STORY", [demo.Beat("Which projects use torch?"), demo.Beat("Make me a water timer.")])
    cli.main(["--demo-run", "--demo-dir", str(tmp_path / "rehearsal")])
    out = capsys.readouterr().out
    assert "Rehearsal mode" in out and "2 of your 4 projects use torch" in out and "lookup itself" in out
    assert "approved on the phone" in out and "widget saved and verified" in out and "Cost: $0.00" in out
    assert (tmp_path / "rehearsal" / "widgets" / "water-timer.html").exists()
    with pytest.raises(demo.DemoError):  # never reuses a folder that already holds a rehearsal
        demo.prepare(tmp_path / "rehearsal")


@pytest.fixture(scope="module")
def chromium():
    pytest.importorskip("playwright.sync_api")
    exe = chromium_executable()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(executable_path=exe).close()
    except Exception as e:
        pytest.skip(f"Chromium not available: {e}")
    return exe


def test_whole_story_offline(tmp_path, chromium):
    cfg = demo.prepare(tmp_path / "rehearsal")
    core, hub, phone = demo.build(cfg)
    hub.start(listen=False)
    lines = []
    try:
        result = demo.run_story(core, hub, phone, out=lines.append)
    finally:
        hub.stop()
        core.shutdown()
    text = "\n".join(lines)
    assert [b["status"] for b in result["beats"]] == ["done"] * len(demo.STORY), text
    # 1. voice note -> form filled by computer use, Submit approved on the phone, verified, then emailed
    assert "form submitted and verified ✓" in text and "email sent, found in Sent ✓" in text
    assert "[Muse Wonder]" in text and text.count("approved on the phone") == 3
    assert any("🎙️" in m["text"] for m in result["phone"]) and any(m.get("buttons") for m in result["phone"])
    assert list((Path(cfg.media_dir) / "outbox").glob("*.eml"))
    # 2. private file: answered locally; the rest of the letter never reached the (scripted) cloud model
    assert "Base salary: INR 24,00,000" in result["beats"][1]["reply"]
    sent = json.dumps(core.agent.client.inner.inner.requests, default=str)
    assert "Joining date" not in sent and "Northwind" not in sent
    # 3. the planted key was redacted before the cloud call, and the audit says so
    assert "AKIADEMO0000LANTERN0" not in sent
    assert result["audit"]["cloud"]["redactions"] == {"aws_access_key": 1}
    assert "redacted before this request left your laptop" in result["beats"][2]["reply"]
    # 4. projects from the local map; 5. the widget passed the checker and was saved after approval
    assert "vision-lab (installed 2.5.1)" in result["beats"][3]["reply"]
    assert (Path(cfg.widgets_dir) / "water-timer.html").exists()
    # honest bookkeeping: zero cost, nothing actually sent anywhere, the repo's demo/home untouched
    assert result["cost_usd"] == 0
    destinations = {r["destination"] for r in core.audit.entries()}
    assert demo.DESTINATION in destinations and not any(d.startswith(("http:", "https:")) for d in destinations)
    assert not (demo.DEMO_HOME / "Documents" / "Lantern").exists()
