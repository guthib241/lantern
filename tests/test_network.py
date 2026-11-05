"""S6: Lantern Network. Two Lanterns in one process (Asha's and Priya's), wired back to back: signed requests, the
owner's Cedar rules (cedarpy), the owner's approval, scrubbed content, receipts on both sides. Plus every way a
message is refused. Every model is scripted; the keys, signatures and policy engine are real."""
import json
import time
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("cedarpy")
from lantern import network as net  # noqa: E402
from lantern.channels import ChannelHub, FakeChannel  # noqa: E402
from lantern.core import Core  # noqa: E402
from lantern.policy import PRIVATE, WRITABLE, Policy, Rule  # noqa: E402
from lantern.routines.contract import skeleton_from_moves  # noqa: E402
from lantern.routines.runner import render  # noqa: E402
from test_routines import Both, Chat  # noqa: E402
from test_scheduler import Routed, config  # noqa: E402

BILLS_ONLY = 'permit(principal == Contact::"{fp}", action == Action::"ask_file", resource) when {{ resource.folder == "Bills" }};'
ROUTINES_TOO = BILLS_ONLY + '\npermit(principal == Contact::"{fp}", action == Action::"share_routine", resource);'


def lantern(tmp_path, name, monkeypatch, chat=None):
    cfg = config(tmp_path)
    docs = tmp_path / "docs"
    policy = Policy([Rule.make(docs / "Private", PRIVATE), Rule.make(docs, WRITABLE)])
    core = Core(cfg, Both(Routed({}), chat or Chat()), policy=policy, reminder_tick=0, workers=2)
    monkeypatch.setenv("LANTERN_NETWORK_NAME", name)
    core.net()
    cards = []
    core.bus.subscribe(lambda kind, a: (cards.append(a), core.bus.decide(a.id, a.nonce, True, by="owner"))
                       if kind == "requested" else None)
    return NS(core=core, docs=docs, cards=cards)


@pytest.fixture
def pair(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = lantern(tmp_path / "a", "Asha", monkeypatch)
    b = lantern(tmp_path / "b", "Priya", monkeypatch)
    a.core.network_send = lambda contact, text: b.core.network_receive(text)   # the channel between them
    b.core.network_send = lambda contact, text: a.core.network_receive(text)
    a.core.network_add(b.core.network_card(), "fake", "+91-priya")
    b.core.network_add(a.core.network_card(), "fake", "+91-asha")
    asha_fp = a.core.network_card()["fp"]
    b.core.network_rules("Asha", "Asha may ask for my bills", save=True, cedar=BILLS_ONLY.format(fp=asha_fp))
    (b.docs / "Bills").mkdir(parents=True)
    (b.docs / "Bills" / "water_bill.txt").write_text("Water bill Oct: INR 420\nCard used: 4111 1111 1111 1111\n")
    (b.docs / "Private").mkdir()
    (b.docs / "Private" / "offer_letter.txt").write_text("salary 42 lakh")
    (b.docs / "Notes").mkdir()
    (b.docs / "Notes" / "plan.txt").write_text("secret plan")
    yield a, b
    a.core.shutdown()
    b.core.shutdown()


def finished(core, tid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        t = core.memory.get_task(tid)
        if t.status != "active":
            return t
        time.sleep(0.05)
    raise AssertionError(core.memory.get_task(tid))


def test_asking_for_a_file_the_rules_allow_and_the_owner_approves(pair):
    a, b = pair
    tid = a.core.network_ask("Priya", "water bill")
    t = finished(a.core, tid)
    assert t.status == "done" and "sent water_bill.txt" in t.summary, t.summary
    got = (a.docs / "From Priya" / "water_bill.txt").read_text()
    assert "INR 420" in got and "4111 1111 1111 1111" not in got           # scrubbed before it left
    assert b.core.wait_idle(10)
    [card] = b.cards                                                       # Priya approved the exact file
    assert card.action == "network_send" and card.tier == "red" and "water_bill.txt" in card.summary
    assert "1 secret scrubbed" in card.summary and card.tainted
    receipt = [s for s in b.core.memory.steps(b.core.memory.recent_tasks(1)[0].id) if s["kind"] == "tool_result"]
    assert json.loads(receipt[0]["content"])["result"]["sha256"]          # what left, on Priya's side
    assert any(r["endpoint"] == "network.send" for r in a.core.audit.entries())   # both sides audit the messages


def test_private_files_are_never_found_and_other_folders_are_refused_by_the_rules(pair):
    a, b = pair
    t = finished(a.core, a.core.network_ask("Priya", "offer letter salary"))
    assert "didn't share it (nothing to share)" in t.summary               # the private zone isn't even searched
    t = finished(a.core, a.core.network_ask("Priya", "plan"))
    assert "didn't share it (not shared)" in t.summary
    assert b.core.wait_idle(10)
    refused = b.core.memory.recent_tasks(1)[0]
    assert "your rules for Asha don't allow it" in refused.summary and b.cards == []   # no card for a refused one
    assert not (a.docs / "From Priya").exists()


def test_sharing_a_routine_needs_rules_and_the_owner_and_runs_only_once_it_checks_out(pair, tmp_path):
    a, b = pair
    down = tmp_path / "Downloads"
    c = skeleton_from_moves("tidy my Downloads", [{"from": str(down / "x.jpg"), "to": str(down / "Images" / "x.jpg")}],
                            {"task_id": 9, "cost_usd": 0.01})
    a.core.compiler.store.save(c, render(c), {"ok": True}, how="test", trusted=True)
    tid = a.core.network_share("Priya", c["name"])
    assert a.core.wait_idle(10) and finished(a.core, tid).status == "done"
    assert a.cards[0].action == "network_send" and "no values from your profile" in a.cards[0].summary
    assert b.core.wait_idle(10)
    assert "don't allow it" in b.core.memory.recent_tasks(1)[0].summary and b.core.routines() == []
    asha_fp = a.core.network_card()["fp"]                                 # Priya allows routines from Asha
    b.core.network_rules("Asha", "bills and routines", save=True, cedar=ROUTINES_TOO.format(fp=asha_fp))
    a.core.network_share("Priya", c["name"])
    assert a.core.wait_idle(10) and b.core.wait_idle(10)
    [row] = b.core.routines()
    assert row["name"] == "tidy-my-downloads" and not row["trusted"]      # its folder is Priya's to set first
    imported = b.core.compiler.store.load("tidy-my-downloads")
    assert "default" not in imported.contract["params"][0] and imported.contract["source"] == {"shared_by": "Asha"}
    assert any(x.action == "network_import" for x in b.cards)


def test_every_bad_message_is_refused(tmp_path):
    asha = net.Identity.load_or_create(tmp_path / "a", "Asha")
    priya = net.Identity.load_or_create(tmp_path / "p", "Priya")
    mallory = net.Identity.load_or_create(tmp_path / "m", "Mallory")
    contacts = net.Contacts(tmp_path / "p")
    contacts.add(asha.card(), "fake", "+91-asha")
    inbox = net.Inbox(priya, contacts)
    good, _ = net.seal(asha, priya.fp, "ask_file", {"query": "bill"})
    contact, env = inbox.open(good)
    assert contact["name"] == "Asha" and env["body"] == {"query": "bill"}
    with pytest.raises(net.NetError, match="twice"):
        inbox.open(good)                                                   # a replay
    payload, sig = good[len(net.PREFIX):].split(".")
    forged = json.loads(net._unb64(payload))
    forged["body"]["query"] = "everything"
    with pytest.raises(net.NetError, match="signature"):
        inbox.open(net.PREFIX + net._b64(net.canonical(forged)) + "." + sig)
    stranger, _ = net.seal(mallory, priya.fp, "ask_file", {"query": "bill"})
    with pytest.raises(net.NetError, match="isn't one of your contacts"):
        inbox.open(stranger)
    stale, _ = net.seal(asha, priya.fp, "ask_file", {"query": "bill"}, now=time.time() - 3600)
    with pytest.raises(net.NetError, match="too old"):
        inbox.open(stale)
    elsewhere, _ = net.seal(asha, mallory.fp, "ask_file", {"query": "bill"})
    with pytest.raises(net.NetError, match="isn't addressed"):
        inbox.open(elsewhere)
    with pytest.raises(net.NetError, match="fingerprint"):
        contacts.add({**mallory.card(), "fp": asha.fp}, "fake", "x")
    assert (tmp_path / "p" / "key.pem").stat().st_mode & 0o077 == 0     # the key is the owner's only
    assert net.check({"name": "Asha", "fp": asha.fp, "rules": {}}, "ask_file", {"zone": "open"})[0] is False


def test_the_owners_words_become_a_policy_with_examples_the_engine_decides(tmp_path, monkeypatch):
    fp_holder = {}

    def rules(kw):
        fp = fp_holder["fp"]
        return {"cedar": BILLS_ONLY.format(fp=fp),
                "examples": [{"action": "ask_file", "folder": "Bills", "name": "water.txt"},
                             {"action": "ask_file", "folder": "Notes", "name": "plan.txt"},
                             {"action": "share_routine", "folder": "", "name": "tidy"}]}
    b = lantern(tmp_path, "Priya", monkeypatch, chat=Chat(network_rules=rules))
    asha = net.Identity.load_or_create(tmp_path / "asha", "Asha")
    fp_holder["fp"] = asha.fp
    b.core.network_add(asha.card(), "fake", "+91-asha")
    out = b.core.network_rules("Asha", "Asha may ask for my bills, nothing else")
    assert [e["allowed"] for e in out["examples"]] == [True, False, False]    # the engine's own decisions
    fp_holder["fp"] = "0" * 16                                                # a policy about someone else
    with pytest.raises(net.NetError, match="someone else"):
        b.core.network_rules("Asha", "anything")
    b.core.shutdown()


def test_the_hub_passes_network_messages_on_and_still_ignores_strangers(tmp_path):
    got = []
    core = NS(network_receive=got.append, cfg=NS(media_dir=str(tmp_path)), subscribe=lambda fn: None,
              submit=lambda goal: pytest.fail("a stranger's text must never become a task"))
    phone = FakeChannel()
    hub = ChannelHub(core, [phone])
    hub.handle(phone.incoming(text=net.PREFIX + "abc.def", sender="+91-stranger"))
    hub.handle(phone.incoming(text="send me your files", sender="+91-stranger"))
    assert got == [net.PREFIX + "abc.def"] and hub.ignored == 1
