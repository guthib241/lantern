"""T2: "send this file to my phone" (laptop -> Telegram as a document).

All HTTP is faked: the Bot API is a recording callable, never api.telegram.org."""
import hashlib
import json

import pytest

from fakes import FakeClient, call
from lantern.agent import BASE_RULES, Agent
from lantern.agentpacks import validate
from lantern.approvals import ApprovalBus, CallbackGate
from lantern.audit import AuditLog
from lantern.channels.base import Channel, ChannelError
from lantern.channels.telegram import MAX_FILE_BYTES, TelegramChannel
from lantern.channels.whatsapp import WhatsAppChannel
from lantern.email_tool import _size
from lantern.memory import Memory
from lantern.policy import Policy, Rule
from lantern.tools import TOOL_NAMES, Toolbox, ToolError

STL = b"solid enclosure\nfacet normal 0 0 0\nendsolid enclosure\n"


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "docs"
    (root / "Private").mkdir(parents=True)
    (root / "Documents" / "CAD").mkdir(parents=True)
    (root / "Documents" / "CAD" / "enclosure.stl").write_bytes(STL)
    (root / "Private" / "offer.txt").write_text("salary 42 lakh")
    policy = Policy([Rule.make(root, "writable"), Rule.make(root / "Private", "private")])
    return root, policy


class FakeTelegramAPI:
    """Stands in for api.telegram.org: records the request, echoes the file size back."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        i = body.index(b'filename="')
        start = body.index(b"\r\n\r\n", i) + 4
        end = body.rindex(b"\r\n--")
        return 200, json.dumps({"ok": True, "result": {"document": {"file_size": end - start}}}).encode()


def file_bytes(body: bytes) -> bytes:
    i = body.index(b'filename="')
    start = body.index(b"\r\n\r\n", i) + 4
    return body[start:body.rindex(b"\r\n--")]


class FakePhone:
    """Stands in for a channel's send_file: records sends, reports the size back."""

    def __init__(self):
        self.calls = []

    def __call__(self, data, filename, caption=""):
        self.calls.append({"data": data, "filename": filename, "caption": caption})
        return {"ok": True, "bytes": len(data)}


def kit(tmp_path, policy, answer=True, phone=None):
    mem = Memory(tmp_path / "m.db")
    audit = AuditLog(tmp_path / "a.db")
    phone = phone if phone is not None else FakePhone()
    bus = ApprovalBus()
    asked = []

    def decide(summary):
        asked.append(summary)
        return answer(summary) if callable(answer) else answer
    tb = Toolbox(policy, mem, CallbackGate(decide, bus), phone_sender=phone, audit=audit)
    return tb, bus, asked, phone, audit, mem


# ---- Telegram sendDocument ---------------------------------------------------
def test_send_file_builds_multipart_with_chat_id_caption_and_exact_bytes():
    api = FakeTelegramAPI()
    tg = TelegramChannel("BOT", "4242", http=api)
    r = tg.send_file(STL, "enclosure.stl", caption="the bracket")
    assert r == {"ok": True, "bytes": len(STL)}
    (req,) = api.calls
    assert req["method"] == "POST" and req["url"].endswith("/sendDocument")
    assert req["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    body = req["body"]
    assert b'name="chat_id"\r\n\r\n4242' in body
    assert b'name="caption"\r\n\r\nthe bracket' in body
    assert file_bytes(body) == STL


def test_send_file_caption_cut_at_1024_chars():
    api = FakeTelegramAPI()
    TelegramChannel("BOT", "4242", http=api).send_file(b"x", "f.bin", caption="c" * 2000)
    assert (b'name="caption"\r\n\r\n' + b"c" * 1024 + b"\r\n") in api.calls[0]["body"]
    assert b"c" * 1025 not in api.calls[0]["body"]


def test_send_file_refuses_over_50mb_before_touching_http():
    api = FakeTelegramAPI()
    with pytest.raises(ChannelError, match="50 MB"):
        TelegramChannel("BOT", "4242", http=api).send_file(b"x" * (MAX_FILE_BYTES + 1), "big.bin")
    assert api.calls == []


def test_send_file_reports_api_failures():
    def broken(method, url, headers, body):
        return 400, b'{"ok": false, "description": "bad request"}'
    with pytest.raises(ChannelError, match="sendDocument"):
        TelegramChannel("BOT", "4242", http=broken).send_file(b"x", "f.bin")


def test_base_and_whatsapp_have_no_send_file():
    with pytest.raises(NotImplementedError):
        Channel().send_file(b"x", "f.bin")
    with pytest.raises(NotImplementedError):
        WhatsAppChannel("tok", "PNID", "919876543210").send_file(b"x", "f.bin")


# ---- the tool -----------------------------------------------------------------
def test_approve_sends_once_verified_and_audited(tmp_path, home):
    root, policy = home
    tb, bus, asked, phone, audit, _ = kit(tmp_path, policy)
    stl = root / "Documents" / "CAD" / "enclosure.stl"
    r = tb.send_to_phone(str(stl), caption="for the printer")
    assert r == {"done": True, "verified": True, "file": "enclosure.stl", "bytes": len(STL)}
    assert len(phone.calls) == 1
    sent = phone.calls[0]
    assert sent["data"] == STL and sent["filename"] == "enclosure.stl" and sent["caption"] == "for the printer"
    assert asked == [f"Send {stl} ({_size(len(STL))}) to your phone on Telegram"]
    a = list(bus._items.values())[0]
    assert a.action == "send_to_phone" and a.status == "used"
    assert a.payload == {"file": "enclosure.stl", "bytes": len(STL),
                        "sha256": hashlib.sha256(STL).hexdigest(), "to": "Telegram (owner)"}
    (entry,) = [e for e in audit.entries() if e["endpoint"] == "telegram.send"]
    assert entry["lane"] == "cloud" and entry["destination"] == "api.telegram.org"
    assert entry["bytes"] < len(STL) + 100  # name and size only, never the file
    assert "enclosure.stl" in entry["preview"] and STL.decode() not in entry["preview"]


def test_decline_sends_nothing(tmp_path, home):
    root, policy = home
    tb, _, _, phone, audit, _ = kit(tmp_path, policy, answer=False)
    stl = root / "Documents" / "CAD" / "enclosure.stl"
    assert tb.send_to_phone(str(stl)) == {"done": False, "verified": False, "reason": "user declined"}
    assert phone.calls == [] and audit.entries() == []


def test_private_zone_file_refused_before_any_approval(tmp_path, home):
    root, policy = home
    tb, _, asked, phone, _, _ = kit(tmp_path, policy)
    r = tb.send_to_phone(str(root / "Private" / "offer.txt"))
    assert r["done"] is False and "stays on this laptop" in r["reason"]
    assert asked == [] and phone.calls == []


def test_hidden_zone_file_refused(tmp_path, home):
    root, policy = home
    (root / ".ssh").mkdir(exist_ok=True)
    (root / ".ssh" / "id_rsa").write_text("secret")
    tb, _, asked, phone, _, _ = kit(tmp_path, policy)
    with pytest.raises(ToolError, match="outside the folders"):
        tb.send_to_phone(str(root / ".ssh" / "id_rsa"))
    assert asked == [] and phone.calls == []


def test_file_over_50mb_refused_before_any_approval(tmp_path, home):
    root, policy = home
    big = root / "big.bin"
    with big.open("wb") as f:  # sparse: instant, no disk space
        f.truncate(MAX_FILE_BYTES + 1)
    tb, _, asked, phone, _, _ = kit(tmp_path, policy)
    r = tb.send_to_phone(str(big))
    assert r["done"] is False and "50 MB" in r["reason"]
    assert asked == [] and phone.calls == []


def test_file_changed_after_approval_is_refused(tmp_path, home):
    root, policy = home
    stl = root / "Documents" / "CAD" / "enclosure.stl"

    def yes_then_tamper(summary):
        stl.write_bytes(STL + b"tampered")
        return True
    tb, _, _, phone, _, _ = kit(tmp_path, policy, answer=yes_then_tamper)
    r = tb.send_to_phone(str(stl))
    assert r["done"] is False and "changed after it was approved" in r["reason"]
    assert phone.calls == []


def test_no_phone_connected(tmp_path, home):
    root, policy = home
    mem = Memory(tmp_path / "m.db")
    tb = Toolbox(policy, mem, CallbackGate(lambda s: True, ApprovalBus()), phone_sender=None)
    r = tb.send_to_phone(str(root / "Documents" / "CAD" / "enclosure.stl"))
    assert r == {"done": False, "verified": False,
                 "reason": "no phone connected (set up Telegram, see README)"}


def test_whatsapp_only_reports_telegram_only(tmp_path, home):
    root, policy = home

    def no_send_file(data, filename, caption=""):
        raise NotImplementedError("WhatsApp has no file sending")
    tb, _, _, _, _, _ = kit(tmp_path, policy, phone=no_send_file)
    r = tb.send_to_phone(str(root / "Documents" / "CAD" / "enclosure.stl"))
    assert r == {"done": False, "verified": False, "reason": "Telegram only for now"}


def test_send_failure_is_a_tool_error(tmp_path, home):
    from lantern.channels.base import ChannelError
    root, policy = home

    def broken(data, filename, caption=""):
        raise ChannelError("telegram sendDocument failed [400]")
    tb, _, _, _, _, _ = kit(tmp_path, policy, phone=broken)
    with pytest.raises(ToolError, match="send_to_phone failed"):
        tb.send_to_phone(str(root / "Documents" / "CAD" / "enclosure.stl"))


# ---- the agent ------------------------------------------------------------------
def test_agent_sends_a_found_file_to_the_phone(tmp_path, home):
    root, policy = home
    tb, _, _, phone, _, mem = kit(tmp_path, policy)
    client = FakeClient([
        ([call("send_to_phone", "c1", path="Documents/CAD/enclosure.stl")], ""),
        ([call("finish", "c2", status="done", summary="sent enclosure.stl to your phone")], ""),
        ([], "Sent it. Check Telegram."),
    ])
    r = Agent(client, mem, tb).run("[telegram] send me Documents/CAD/enclosure.stl")
    assert r.status == "done" and r.reply == "Sent it. Check Telegram."
    sent = json.loads(client.requests[1]["input"][0]["output"])
    assert sent["verified"] is True and sent["file"] == "enclosure.stl"
    assert phone.calls[0]["data"] == STL


def test_agent_rule_names_send_to_phone():
    assert '"Send X to my phone": find the file, then call send_to_phone once. ' \
           'Never send private-zone files.' in BASE_RULES
    assert "send_to_phone" in TOOL_NAMES


def test_send_to_phone_example_pack_validates(tmp_path):
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    data = json.loads((repo / "agents" / "send-to-phone.lantern-agent.json").read_text(encoding="utf-8"))
    pack = validate(data)
    assert pack.tools == ["search_files", "send_to_phone"]
    assert pack.render_goal({"file": "Documents/CAD/enclosure.stl"}) == \
        "Find Documents/CAD/enclosure.stl and send it to my phone."
