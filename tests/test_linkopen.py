"""T1: open a shared link on the laptop after approval (all offline)."""
import hashlib

from fakes import FakeClient, call
from lantern.agent import Agent
from lantern.approvals import ApprovalBus, CallbackGate
from lantern.channels import ChannelHub, FakeChannel
from lantern.config import Config
from lantern.core import Core
from lantern.linkopen import clean_url
from lantern.memory import Memory
from lantern.tools import Toolbox


# ---- clean_url -----------------------------------------------------------------
def test_clean_url_share_text_with_title():
    assert clean_url("Cool video https://youtu.be/abc?si=x") == "https://youtu.be/abc?si=x"


def test_clean_url_keeps_query_and_strips_trailing_punctuation():
    assert clean_url("watch https://youtu.be/abc?si=x).") == "https://youtu.be/abc?si=x"
    assert clean_url("see https://example.com/page,") == "https://example.com/page"


def test_clean_url_rejects_non_web_schemes():
    assert clean_url("javascript:alert(1)") is None
    assert clean_url("file:///C:/x") is None
    assert clean_url("data:text/html,hi") is None
    assert clean_url("intent://x#Intent;end") is None


def test_clean_url_rejects_userinfo_and_long_urls():
    assert clean_url("https://user:pw@x.com") is None
    assert clean_url("no link here at all") is None
    assert clean_url("https://x.com/" + "a" * 2048) is None


# ---- tool ----------------------------------------------------------------------
def kit(tmp_path, answer=True):
    mem = Memory(tmp_path / "m.db")
    bus = ApprovalBus()
    asked = []

    def decide(summary):
        asked.append(summary)
        return answer(summary) if callable(answer) else answer

    tb = Toolbox([tmp_path], mem, CallbackGate(decide, bus))
    return tb, bus, asked


def test_open_link_approved_opens_exact_url(tmp_path):
    tb, bus, asked = kit(tmp_path)
    opened = []
    tb.link_opener = lambda url: opened.append(url) or True
    r = tb.open_link("Cool video https://youtu.be/abc?si=x")
    assert opened == ["https://youtu.be/abc?si=x"]
    assert r == {"done": True, "verified": True, "url": "https://youtu.be/abc?si=x"}
    assert len(asked) == 1 and "https://youtu.be/abc?si=x" in asked[0]
    a = list(bus._items.values())[0]
    assert a.action == "open_link"
    assert a.payload == {"url": "https://youtu.be/abc?si=x",
                         "url_sha256": hashlib.sha256(b"https://youtu.be/abc?si=x").hexdigest()}


def test_open_link_declined_never_opens(tmp_path):
    tb, _, _ = kit(tmp_path, answer=False)
    opened = []
    tb.link_opener = lambda url: opened.append(url) or True
    assert tb.open_link("https://youtu.be/abc") == {"done": False, "reason": "user declined"}
    assert opened == []


def test_open_link_non_web_refused_before_approval(tmp_path):
    tb, _, asked = kit(tmp_path)
    opened = []
    tb.link_opener = lambda url: opened.append(url) or True
    assert tb.open_link("javascript:alert(1)") == {"done": False, "reason": "not a web link (only http/https)"}
    assert asked == [] and opened == []


# ---- agent ---------------------------------------------------------------------
def test_agent_calls_open_link_for_phone_goal(tmp_path):
    mem = Memory(tmp_path / "m.db")
    tb = Toolbox([tmp_path], mem, lambda summary: True)
    opened = []
    tb.link_opener = lambda url: opened.append(url) or True
    agent = Agent(FakeClient([
        ([call("open_link", "c1", url="https://youtu.be/abc")], ""),
        ([call("finish", "c2", status="done", summary="opened the video")], ""),
        ([], "Opened it on your laptop."),
    ]), mem, tb)
    agent.run("[telegram] open this on my laptop https://youtu.be/abc")
    assert opened == ["https://youtu.be/abc"]


# ---- hub fast path ---------------------------------------------------------------
def make_hub(tmp_path, script):
    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path, cfg.zones_file = str(tmp_path / "a.db"), str(tmp_path / "z.json")
    cfg.media_dir, cfg.profile_path = str(tmp_path / "media"), str(tmp_path / "profile.json")
    core = Core(cfg, FakeClient(script), approval_timeout=10)
    phone = FakeChannel()
    hub = ChannelHub(core, [phone]).start(listen=False)
    return core, hub, phone


def settle(core, hub):
    assert hub.wait_idle(10) and core.wait_idle(10)


def test_hub_bare_link_from_owner_submits_task(tmp_path):
    core, hub, phone = make_hub(tmp_path, [([], "Opened it.")])
    try:
        hub.receive(phone.incoming("https://youtu.be/abc"))
        settle(core, hub)
        (task,) = core.memory.recent_tasks()
        assert task.goal == "[fake] open this link on my laptop: https://youtu.be/abc"
    finally:
        core.shutdown()


def test_hub_link_from_stranger_is_ignored(tmp_path):
    core, hub, phone = make_hub(tmp_path, [([], "Opened it.")])
    try:
        hub.receive(phone.incoming("https://youtu.be/abc", sender="stranger"))
        settle(core, hub)
        assert hub.ignored == 1 and core.memory.recent_tasks() == []
    finally:
        core.shutdown()
