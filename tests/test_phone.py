"""Better WhatsApp messages: approval cards (cards.py), list menus + quick replies,
Muse Spark follow-ups (suggest.py) and the daily summary (daily.py). All offline."""
import json
import time

from fakes import FakeClient
from lantern import models
from lantern.approvals import ApprovalBus
from lantern.budget import Limits
from lantern.channels import ChannelHub, FakeChannel, TelegramChannel, WhatsAppChannel, approval_card
from lantern.channels import daily as daily_mod
from lantern.channels import suggest as suggest_mod
from lantern.channels.base import Channel
from lantern.config import Config
from lantern.core import Core
from test_channels import OWNER, button, make, payload, post, settle, text, wait_for
from test_routines import Both, Chat

SUGGESTIONS = {"suggestions": [{"title": "Send it again", "request": "Send the report to Priya every Friday"},
                               {"title": "File it", "request": "File the report in Documents"}]}
SUMMARY_LINES = {"lines": ["Sun is out.", "Milk done."]}


def make_fake(tmp_path, script, **chat_handlers):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "a.db"), str(tmp_path / "z.json")
    cfg.media_dir, cfg.profile_path = str(tmp_path / "media"), str(tmp_path / "profile.json")
    client = Both(FakeClient(script), Chat(**chat_handlers)) if chat_handlers else FakeClient(script)
    core = Core(cfg, client, approval_timeout=10)
    phone = FakeChannel()
    hub = ChannelHub(core, [phone]).start(listen=False)
    return core, hub, phone


def texts(phone):
    return [m.get("text", "") for m in phone.messages() if m.get("type") != "list"]


# ---- 1. approval cards ------------------------------------------------------------
def email_approval(**kw):
    bus = ApprovalBus()
    return bus.request("send_email", "Send email to priya@example.com",
                       {"from": "me@example.com", "to": ["priya@example.com"], "cc": [],
                        "subject": "Slides", "body_sha256": "abc", "body_chars": 12,
                        "attachments": [{"name": "s.pdf", "sha256": "d", "bytes": 10, "private": False}]},
                       tainted=False, tier="red", flags=[])


def test_card_red_email_first_line_facts_and_footer():
    a = email_approval()
    card = approval_card(a, a.created_at + 60)
    lines = card.text.splitlines()
    assert lines[0] == "🔴 Send email"
    assert "To: priya@example.com" in lines
    assert "Subject: Slides" in lines
    assert "Attachments: 1 (s.pdf)" in lines
    assert card.footer == f"Lantern needs your OK · expires in 9 min · #{a.id}"
    assert len(card.text) <= 1024 and len(card.footer) <= 60
    assert [t for _, t in card.buttons] == ["✅ Approve", "❌ Deny", "🔎 Show"]
    assert all(len(t) <= 20 for _, t in card.buttons)
    assert card.buttons[0][0] == f"approve:{a.id}:{a.nonce}:{a.payload_hash[:8]}"


def test_card_yellow_write_file_and_dict_input():
    bus = ApprovalBus()
    a = bus.request("write_file", "Create file todo.txt",
                    {"path": "/docs/todo.txt", "sha256": "abc", "chars": 8, "overwrite": False}, tier="yellow")
    card = approval_card(a.public(include_nonce=True), a.created_at)
    assert card.text.splitlines()[0] == "🟡 Create file"
    assert "File: /docs/todo.txt" in card.text.splitlines()
    assert "abc" not in card.text   # hashes never shown


def test_card_untrusted_and_flags_get_their_own_lines():
    a = email_approval()
    a.tainted = True
    a.flags = ["⚠️ priya@example.com appeared only in a file, not in your request"]
    card = approval_card(a, a.created_at)
    assert "⚠️ came from a web page/file, not from you" in card.text.splitlines()
    assert a.flags[0] in card.text


def test_card_generic_action_falls_back_to_payload_fields():
    bus = ApprovalBus()
    a = bus.request("remember", "Remember x", {"key": "manager", "value": "Priya"}, tier="yellow")
    card = approval_card(a, a.created_at)
    assert card.text.splitlines()[0] == "🟡 Remember"
    assert "Fact: manager" in card.text.splitlines()


def test_card_tidy_moves_show_short_paths():
    bus = ApprovalBus()
    a = bus.request("organize_files", "Move 6 files",
                    {"moves": [{"from": f"/tmp/x/home/Downloads/f{i}.txt",
                                "to": f"/tmp/x/home/Downloads/Images/f{i}.txt"} for i in range(2)]})
    card = approval_card(a, a.created_at)
    assert card.text.splitlines()[0] == "🟡 Tidy files"
    assert "Files: 2" in card.text.splitlines()
    assert "→ home/Downloads/Images (2)" in card.text
    assert "→ /" not in card.text


def test_card_on_whatsapp_puts_expiry_in_footer(tmp_path):
    core, hub, graph, http, _ = make(tmp_path, [
        ([__import__("fakes").call("write_file", "c1", path="todo.txt", content="buy milk")], ""),
        ([], "Done."),
    ])
    post(http, payload(text("add milk")))
    assert wait_for(lambda: graph.sent("interactive"))
    msg = graph.sent("interactive")[0]["interactive"]
    assert msg["type"] == "button"
    assert msg["body"]["text"].splitlines()[0] == "🟡 Create file"
    assert "Lantern needs your OK" in msg["footer"]["text"]
    assert "expires in" in msg["footer"]["text"] and len(msg["footer"]["text"]) <= 60
    deny = next(b["reply"]["id"] for b in msg["action"]["buttons"] if b["reply"]["title"] == "❌ Deny")
    post(http, payload(button(deny, mid="wamid.d")))
    settle(core, hub)


# ---- 2. list menus ------------------------------------------------------------------
def test_whatsapp_list_shape_and_limits():
    seen = {}

    def http(method, url, headers, body):
        seen["json"] = json.loads(body)
        return 200, b'{"messages": [{"id": "wamid.out"}]}'

    wa = WhatsAppChannel("tok", "PNID", OWNER, http=http)
    wa.send_list("Pick one", "Menu", [("menu:status", "Status", "What I do"), ("menu:stop", "Stop", "")])
    li = seen["json"]["interactive"]
    assert li["type"] == "list" and li["body"]["text"] == "Pick one"
    assert li["action"]["button"] == "Menu" and len(li["action"]["sections"][0]["rows"]) == 2
    row = li["action"]["sections"][0]["rows"][0]
    assert row == {"id": "menu:status", "title": "Status", "description": "What I do"}
    try:
        wa.send_list("x", "Menu", [])
        raise AssertionError("empty list must fail")
    except ValueError:
        pass


def test_whatsapp_list_reply_parses_to_button():
    wa = WhatsAppChannel("tok", "PNID", OWNER)
    msg = {"from": OWNER, "id": "wamid.l", "timestamp": "1", "type": "interactive",
           "interactive": {"type": "list_reply",
                           "list_reply": {"id": "menu:status", "title": "Status", "description": "x"}}}
    (inc,) = wa.parse({"object": "whatsapp_business_account",
                       "entry": [{"changes": [{"value": {"messages": [msg]}}]}]})
    assert (inc.kind, inc.button) == ("button", "menu:status")


def test_list_reply_row_runs_menu_over_webhook(tmp_path):
    core, hub, graph, http, _ = make(tmp_path, [])
    tap = {"from": OWNER, "id": "wamid.l2", "timestamp": "1", "type": "interactive",
           "interactive": {"type": "list_reply", "list_reply": {"id": "menu:status", "title": "Status"}}}
    post(http, payload(tap))
    settle(core, hub)
    assert any(t.startswith("Idle.") for t in graph.texts())


def test_telegram_and_fake_lists_and_base_fallback():
    calls = []

    def http(method, url, headers, body):
        calls.append(json.loads(body))
        return 200, json.dumps({"ok": True, "result": {}}).encode()

    tg = TelegramChannel("BOT", "4242", http=http)
    tg.send_list("Pick", "Menu", [("menu:status", "Status", ""), ("menu:stop", "Stop everything", "")])
    sent = [c for c in calls if c.get("reply_markup")][0]
    row = [b for line in sent["reply_markup"]["inline_keyboard"] for b in line]
    assert [b["text"] for b in row] == ["Status", "Stop everything"]
    assert [b["callback_data"] for b in row] == ["menu:status", "menu:stop"]

    phone = FakeChannel()
    phone.send_list("Pick", "Menu", [("a", "A", "first"), ("b", "B", "")])
    (listed,) = [m for m in phone.messages() if m["type"] == "list"]
    assert (listed["button"], [r["id"] for r in listed["rows"]]) == ("Menu", ["a", "b"])

    class Plain(Channel):
        name = "plain"

        def __init__(self):
            self.got = []

        def send_text(self, text):
            self.got.append(text)

        def send_buttons(self, text, buttons):
            raise AssertionError("fallback must be plain text")

    p = Plain()
    p.send_list("Pick", "Menu", [("a", "Apples", ""), ("b", "Bread", "")])
    assert p.got == ["Pick\n1. Apples\n2. Bread"]


# ---- menu + stop confirmation ---------------------------------------------------------
def test_menu_lists_and_rows_work(tmp_path):
    core, hub, phone = make_fake(tmp_path, [])
    hub.receive(FakeChannel.incoming("menu"))
    assert hub.wait_idle(10)
    (listed,) = [m for m in phone.messages() if m["type"] == "list"]
    assert [r["title"] for r in listed["rows"]] == ["Status", "Reminders", "Undo last task",
                                                   "Today's summary", "Stop everything"]
    hub.receive(FakeChannel.incoming("hi"))
    assert hub.wait_idle(10)
    assert sum(1 for m in phone.messages() if m["type"] == "list") == 2
    hub.receive(FakeChannel.incoming(button="menu:reminders"))
    assert hub.wait_idle(10)
    assert texts(phone)[-1] == "No reminders pending."
    hub.receive(FakeChannel.incoming("1"))   # the typed number picks Status
    assert hub.wait_idle(10)
    assert texts(phone)[-1].startswith("Idle.")
    assert [t.id for t in core.memory.recent_tasks()] == []   # menu taps never start tasks
    core.shutdown()
    hub.stop()


def test_stop_row_asks_first(tmp_path):
    core, hub, phone = make_fake(tmp_path, [])
    hub.receive(FakeChannel.incoming(button="menu:stop"))
    assert hub.wait_idle(10)
    card = [m for m in phone.messages() if m["type"] == "buttons"][-1]
    assert card["text"] == "Stop all tasks?"
    assert [b["title"] for b in card["buttons"]] == ["Yes", "No"]
    hub.receive(FakeChannel.incoming(button="menu:stop:no"))
    assert hub.wait_idle(10)
    assert texts(phone)[-1] == "Okay, still running."
    hub.receive(FakeChannel.incoming(button="menu:stop:yes"))
    assert hub.wait_idle(10)
    assert texts(phone)[-1].startswith("🛑 Stopped. Nothing was running.")
    core.shutdown()
    hub.stop()


# ---- suggestions -------------------------------------------------------------------------
def test_done_task_gets_suggestions_and_tap_starts_task(tmp_path):
    chat = Chat(phone_suggestions=lambda kw: SUGGESTIONS)
    core, hub, graph, http, _ = make(tmp_path, [
        ([], "Milk is on the list."),
        ([], "Second reply."),
    ], chat=chat)
    post(http, payload(text("buy milk")))
    assert wait_for(lambda: len(graph.sent("interactive")) >= 1)
    settle(core, hub)
    sugg = [m for m in graph.sent("interactive") if m["interactive"]["body"]["text"].startswith("💡")]
    assert len(sugg) == 1
    ids = {b["reply"]["title"]: b["reply"]["id"] for b in sugg[0]["interactive"]["action"]["buttons"]}
    assert set(ids) == {"Send it again", "File it"} and all(len(t) <= 20 for t in ids)
    sent = chat.of("phone_suggestions")[-1]   # what Spark was asked, through the audited client
    assert sent["model"] == models.SPARK
    asked = json.loads(sent["messages"][1]["content"])
    assert asked["goal"] == "buy milk" and asked["reply"] == "Milk is on the list."
    assert len(hub._suggestions) == 2
    post(http, payload(button(ids["Send it again"], mid="wamid.s1")))
    settle(core, hub)
    tasks = core.memory.recent_tasks()
    assert tasks[0].goal == "[whatsapp] Send the report to Priya every Friday"
    assert f"🏮 On it (task #{tasks[0].id})." in graph.texts()
    assert ids["Send it again"].split(":")[1] not in hub._suggestions   # the tap consumed its id
    core.shutdown()
    hub.stop()


def test_suggestions_shaped_in_code():
    assert suggest_mod.shape({"suggestions": [
        {"title": "A very long button title here", "request": "  Do the thing  "},
        {"title": "", "request": "dropped"}, {"title": "x", "request": ""}]}) == [
        ("A very long button t", "Do the thing")]
    assert suggest_mod.shape({}) == [] and suggest_mod.shape(None) == []
    assert suggest_mod.enabled({}) and not suggest_mod.enabled({"LANTERN_PHONE_SUGGESTIONS": "0"})


def test_no_suggestions_for_private_replies(tmp_path):
    core, hub, phone = make_fake(tmp_path, [])
    task = core.memory.start_task("[fake] read the private file")
    hub.origin[task.id] = "fake"
    hub._on_event({"type": "done", "id": task.id, "status": "done",
                   "reply": "Your balance is 12,000", "reply_public": "(answered from a private file)",
                   "summary": "s"})
    settle(core, hub)
    assert not [m for m in phone.messages() if m.get("type") == "buttons"]
    assert not hub._suggestions
    core.shutdown()
    hub.stop()


def test_no_suggestions_after_untrusted_content_and_full_requests_are_shown(tmp_path):
    """A task that read a web page or file could pass planted words into a suggestion: no suggestions then. When
    suggestions do go out, the full request is on screen, so a tap sends nothing the owner didn't read."""
    core, hub, phone = make_fake(tmp_path, [], phone_suggestions=lambda kw: SUGGESTIONS)
    def done(tainted):
        task = core.memory.start_task("[fake] summarise this page")
        hub.origin[task.id] = "fake"
        event = {"type": "done", "id": task.id, "status": "done", "reply": "The page says hi.", "summary": "s"}
        if tainted is not None:
            event["tainted"] = tainted
        hub._on_event(event)
    done(True)    # read untrusted content
    done(None)    # a done event that doesn't say
    settle(core, hub)
    time.sleep(0.3)
    assert not hub._suggestions and not [m for m in phone.messages() if m.get("type") == "buttons"]
    done(False)
    assert wait_for(lambda: [m for m in phone.messages() if m.get("type") == "buttons"])
    [msg] = [m for m in phone.messages() if m.get("type") == "buttons"]
    assert "Send the report to Priya every Friday" in msg["text"]
    core.shutdown()
    hub.stop()


def test_no_suggestions_when_off_or_over_budget(tmp_path, monkeypatch):
    core, hub, phone = make_fake(tmp_path, [])
    task = core.memory.start_task("[fake] buy milk")
    hub.origin[task.id] = "fake"
    monkeypatch.setenv("LANTERN_PHONE_SUGGESTIONS", "0")
    hub._on_event({"type": "done", "id": task.id, "status": "done",
                   "reply": "Milk is on the list.", "summary": "s"})
    settle(core, hub)
    assert not hub._suggestions
    monkeypatch.setenv("LANTERN_PHONE_SUGGESTIONS", "1")
    core.memory.log_usage(task.id, models.SPARK, cost_usd=5.0)
    core.budget.limits = Limits(total=None, daily=None, task=1.0)
    assert suggest_mod.over_budget(core, task.id)
    hub._on_event({"type": "done", "id": task.id, "status": "done",
                   "reply": "Milk is on the list.", "summary": "s"})
    settle(core, hub)
    assert not hub._suggestions   # the script is empty: any call would have raised
    core.shutdown()
    hub.stop()


# ---- daily summary --------------------------------------------------------------------------
def at_today(h, m):
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))


def test_daily_due_parsing():
    assert daily_mod.summary_at({"LANTERN_DAILY_SUMMARY": "21:00"}) == (21, 0)
    assert daily_mod.summary_at({"LANTERN_DAILY_SUMMARY": "off"}) is None
    assert daily_mod.summary_at({"LANTERN_DAILY_SUMMARY": "99:99"}) is None
    assert daily_mod.due(set(), at_today(22, 0), {"LANTERN_DAILY_SUMMARY": "21:00"})
    assert not daily_mod.due(set(), at_today(20, 0), {"LANTERN_DAILY_SUMMARY": "21:00"})
    today = time.strftime("%Y-%m-%d", time.localtime(at_today(22, 0)))
    assert not daily_mod.due({today}, at_today(22, 0), {"LANTERN_DAILY_SUMMARY": "21:00"})
    assert not daily_mod.due(set(), at_today(22, 0), {"LANTERN_DAILY_SUMMARY": "off"})


def test_daily_gather_and_model_and_fallback(tmp_path):
    core, hub, phone = make_fake(tmp_path, [], daily_summary=lambda kw: SUMMARY_LINES)
    done = core.memory.start_task("[whatsapp] buy milk")
    core.memory.finish_task(done.id, "done", "Milk is on the list.")
    core.memory.log_step(done.id, "tool_result", {"name": "write_file", "result": {"verified": True}})
    core.memory.mark_private(done.id)
    core.memory.add_reminder("call Priya", at_today(23, 0) + 86400)
    core.memory.log_usage(done.id, models.SPARK, cost_usd=0.01)
    core.memory.log_step(done.id, "approval", {"event": "decided", "status": "denied",
                                               "action": "send_email", "summary": "Send it"})
    facts = daily_mod.gather_facts(core)
    assert facts["tasks"][0]["verified"] and "summary" not in facts["tasks"][0]  # private: no reply
    assert facts["reminders"] and facts["spend_today_usd"] > 0 and len(facts["denied"]) == 1
    assert daily_mod.write(core, facts) == "Sun is out.\nMilk done."
    usage = [u for u in core.memory.usage_by_model() if u["model"] == models.SPARK]
    assert usage and usage[0]["cost_usd"] > 0   # the summary call is logged like other calls
    core.shutdown()
    hub.stop()

    core2, hub2, _ = make_fake(tmp_path, [])   # empty script: the model call fails -> fallback
    assert daily_mod.write(core2, daily_mod.gather_facts(core2)) is None
    assert daily_mod.render(core2).startswith("☀️ Today:")
    core2.shutdown()
    hub2.stop()


def test_daily_summary_command_and_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("LANTERN_DAILY_SUMMARY", "21:00")
    core, hub, phone = make_fake(tmp_path, [], daily_summary=lambda kw: {"lines": ["All quiet today."]})
    hub.receive(FakeChannel.incoming("summary"))
    assert hub.wait_idle(10)
    assert texts(phone)[-1] == "All quiet today."
    assert hub.check_daily(now=at_today(22, 0))   # scheduled: once a day, to every channel
    assert hub.wait_idle(10)
    assert texts(phone)[-1] == "All quiet today."
    assert not hub.check_daily(now=at_today(22, 30))
    core.shutdown()
    hub.stop()

    core2, hub2, phone2 = make_fake(tmp_path, [])   # a restart the same evening must not send it again
    assert not hub2.check_daily(now=at_today(22, 0))
    assert hub2.wait_idle(10)
    assert texts(phone2) == []
    assert hub2.check_daily(now=at_today(22, 0) + 86400)   # next day, no chat client: the fallback goes out
    assert hub2.wait_idle(10)
    assert texts(phone2)[-1].startswith("☀️ Today:")
    core2.shutdown()
    hub2.stop()
