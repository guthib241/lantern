"""S13 Lend your Lantern. Amma gets an hour on the owner's Lantern ("read my bills; never the Bank folder"): her
messages and voice notes get through only while the loan is active; each step is checked against the grant's Cedar
policy (real cedarpy) and approved by the owner on the phone; the Bank and private folders are refused without a
card; she gets the reply and the owner the receipt. Loans end on time or at once when revoked, stopping what's
running. Every model is scripted."""
import time

import pytest

pytest.importorskip("cedarpy")
from fakes import call  # noqa: E402
from lantern import lend as lend_mod  # noqa: E402
from lantern.channels import ChannelHub, FakeChannel  # noqa: E402
from lantern.core import Core  # noqa: E402
from lantern.policy import PRIVATE, WRITABLE, Policy, Rule  # noqa: E402
from test_routines import Both, Chat  # noqa: E402
from test_scheduler import Routed, config, finish, until  # noqa: E402

AMMA = "+91 98450 11111"
LOAN = ('permit(principal == Borrower::"borrower", action in [Action::"list_dir", Action::"search_files", '
        'Action::"read_file"], resource);\n'
        'forbid(principal, action, resource) when { resource.folders.contains("Bank") };')


def home(tmp_path, scripts, chat=None, approve=True):
    docs = tmp_path / "docs"
    for d in ("Bills", "Bank", "Private"):
        (docs / d).mkdir(parents=True, exist_ok=True)
    (docs / "Bills" / "water_bill.txt").write_text("Water bill, October: INR 420, due 5 Nov\n")
    (docs / "Bank" / "statement.txt").write_text("Balance INR 2,40,000\n")
    (docs / "Private" / "diary.txt").write_text("private")
    policy = Policy([Rule.make(docs / "Private", PRIVATE), Rule.make(docs, WRITABLE)])
    core = Core(config(tmp_path), Both(Routed(scripts), chat or Chat()), policy=policy, reminder_tick=0, workers=2)
    cards = []

    def decide(kind, a):
        if kind == "requested":
            cards.append(a)
            if approve:
                core.bus.decide(a.id, a.nonce, True, by="owner")
    core.bus.subscribe(decide)
    phone = FakeChannel()
    hub = ChannelHub(core, [phone], transcriber=lambda wav, keywords=None: {"transcript": "Amma here: what is my "
                                                                             "water bill", "audioDurationMs": 3000},
                     converter=lambda src, dst: src).start(listen=False)
    return core, hub, phone, cards, docs


def to(phone, who):
    return [m["text"] for m in phone.messages() if m.get("to") == who]


def owner(phone):
    return [m["text"] for m in phone.messages() if "to" not in m]


def test_a_loan_lets_amma_read_bills_with_the_owners_ok_and_never_the_bank(tmp_path):
    docs = tmp_path / "docs"
    core, hub, phone, cards, _ = home(tmp_path, {"water": [
        ([call("read_file", "r1", path=str(docs / "Bills" / "water_bill.txt"))], ""),
        ([call("read_file", "r2", path=str(docs / "Bank" / "statement.txt"))], ""),
        ([call("read_file", "r3", path=str(docs / "Private" / "diary.txt"))], ""),
        finish("r4", "The water bill is INR 420, due 5 Nov."),
        ([], "Your water bill is INR 420, due on 5 November."),
    ]})
    g = core.lend("Amma", "fake", AMMA, "may read my bills for 1 hour; never the Bank folder", save=True, cedar=LOAN,
                  hours=1)
    assert until(lambda: to(phone, AMMA))
    assert to(phone, AMMA)[0].startswith("🏮 This Lantern is lent to you until")
    hub.handle(phone.incoming(text="what is my water bill?", sender="+91-98450-11111"))   # same number, other format
    tid = next(iter(hub.borrowed))
    assert core.wait_idle(30)
    t = core.memory.get_task(tid)
    assert t.status == "done" and t.goal.startswith("[lent] Amma, who has borrowed this Lantern, asks:")
    [card] = cards                                              # one card: the bill; the Bank and Private never asked
    assert card.action == "lent_action" and card.tier == "red" and "Amma (lent until" in card.summary
    assert "read a file" in card.summary and "water_bill.txt" in card.summary
    calls = next(s for s in core.memory.steps(tid) if s["kind"] == "lend")["content"]
    assert '"allowed": false' in calls and "doesn't allow" in calls and "never lent" in calls
    assert until(lambda: any("INR 420" in m for m in to(phone, AMMA)))
    assert until(lambda: any(m.startswith("🔑 Amma's task") for m in owner(phone)))
    receipt = next(m for m in owner(phone) if m.startswith("🔑 Amma's task"))
    assert "🧾 Task #" in receipt and "lent_action" in receipt and "Cost:" in receipt   # the full receipt
    assert any("Amma asked your Lantern" in m for m in owner(phone))
    assert g["id"] in core.lending().all() and core.lending().get(g["id"])["tasks"] == [tid]
    with pytest.raises(ValueError, match="borrower"):
        core.retry_task(tid)                                    # never run again outside the loan
    hub.stop()
    core.shutdown()


def test_strangers_and_ended_loans_get_nothing_and_revoking_stops_what_runs(tmp_path):
    docs = tmp_path / "docs"
    core, hub, phone, cards, _ = home(tmp_path, {"bill": [
        ([call("list_dir", "l1", path=str(docs / "Bills"))], ""), finish("l2"), ([], "ok")]}, approve=False)
    hub.handle(phone.incoming(text="show me the bills", sender=AMMA))          # no loan yet: a stranger
    assert hub.ignored == 1 and core.memory.recent_tasks(1) == []
    g = core.lend("Amma", "fake", AMMA, "bills for an hour", save=True, cedar=LOAN, hours=1)
    hub.handle(phone.incoming(text="show me the bills", sender=AMMA))
    assert until(lambda: len(cards) == 1)                       # waiting on the owner's card
    tid = next(iter(hub.borrowed))
    core.revoke(g["id"])
    assert core.wait_idle(20)
    assert cards[0].status == "denied" and core.memory.get_task(tid).status != "done"
    assert until(lambda: any("was revoked" in m for m in to(phone, AMMA)))
    hub.handle(phone.incoming(text="again please", sender=AMMA))
    assert hub.ignored == 2
    g2 = core.lend("Amma", "fake", AMMA, "bills", save=True, cedar=LOAN, hours=0.25)
    assert core.expire_grants(now=time.time() + 3600) == [g2["id"]]
    assert core.lending().get(g2["id"])["status"] == "expired"
    assert until(lambda: any("has ended" in m for m in to(phone, AMMA)))
    hub.handle(phone.incoming(text="one more", sender=AMMA))
    assert hub.ignored == 3
    hub.stop()
    core.shutdown()


def test_a_voice_note_from_amma_is_transcribed_and_answered_in_her_chat(tmp_path):
    core, hub, phone, cards, docs = home(tmp_path, {"water": [finish("v1", "INR 420"), ([], "INR 420, due 5 Nov.")]})
    core.lend("Amma", "fake", AMMA, "bills", save=True, cedar=LOAN, hours=1)
    hub.handle(phone.incoming(audio=b"OggS...", sender=AMMA))
    assert core.wait_idle(30)
    assert until(lambda: any(m.startswith("🎙️ “Amma here") for m in to(phone, AMMA)))
    assert not any(m.startswith("🎙️") for m in owner(phone))      # her words echo in her chat, not the owner's
    assert core.memory.recent_tasks(1)[0].goal.endswith("what is my water bill")
    hub.stop()
    core.shutdown()


def test_the_owners_words_become_a_policy_whose_examples_the_engine_decides(tmp_path):
    def rules(kw):
        return {"cedar": LOAN, "hours": 99, "examples": [
            {"action": "read_file", "path": "/home/a/Documents/Bills/water.txt", "host": ""},
            {"action": "read_file", "path": "/home/a/Documents/Bank/statement.txt", "host": ""},
            {"action": "send_email", "path": "", "host": ""}]}
    out = lend_mod.compile_grant(Both(None, Chat(lend_rules=rules)), "Amma", "bills; never the Bank")
    assert [e["allowed"] for e in out["examples"]] == [True, False, False] and out["hours"] == 24.0
    bad = Chat(lend_rules=lambda kw: {"cedar": 'permit(principal == Contact::"x", action, resource);', "hours": 1,
                                      "examples": []})
    with pytest.raises(lend_mod.LendError, match="someone else"):
        lend_mod.compile_grant(Both(None, bad), "Amma", "anything")


def test_a_principal_written_with_is_is_read_as_the_one_borrower_it_names(tmp_path):
    """Seen live: Muse Spark wrote `principal is Borrower::"borrower"` (Cedar's `is` takes a type), so the grant was
    refused as unparseable. It is read as `==` that one entity; someone else is still refused afterwards."""
    live = ('permit(principal is Borrower::"borrower", action == Action::"fill_form", resource) when { resource.kind '
            '== "web" && resource.host == "portal.school.example" }; permit(principal is Borrower::"borrower", action '
            '== Action::"read_file", resource) when { resource.kind == "file" && resource.folder == "Shared" };')
    chat = Chat(lend_rules=lambda kw: {"cedar": live, "hours": 3, "examples": [
        {"action": "fill_form", "path": "", "host": "portal.school.example"},
        {"action": "read_file", "path": "/home/a/Shared/trip.pdf", "host": ""},
        {"action": "read_file", "path": "/home/a/Bills/water.txt", "host": ""}]})
    out = lend_mod.compile_grant(Both(None, chat), "Ravi", "the school trip form and my Shared folder for 3 hours")
    assert 'principal is' not in out["cedar"] and out["cedar"].count('principal == Borrower::"borrower"') == 2
    assert [e["allowed"] for e in out["examples"]] == [True, True, False] and out["hours"] == 3.0
    assert 'principal == Borrower::"borrower", action == Action::"read_file"' in chat.requests[0]["messages"][0]["content"]
    other = Chat(lend_rules=lambda kw: {"cedar": 'permit(principal is Contact::"x", action, resource);', "hours": 1,
                                        "examples": []})
    with pytest.raises(lend_mod.LendError, match="someone else"):
        lend_mod.compile_grant(Both(None, other), "Ravi", "anything")


def test_the_guard_refuses_in_code_before_any_policy(tmp_path):
    docs = tmp_path / "docs"
    (docs / "Private").mkdir(parents=True)
    policy = Policy([Rule.make(docs / "Private", PRIVATE), Rule.make(docs, WRITABLE)])
    g = {"name": "Amma", "status": "active", "expires_at": time.time() + 60,
         "cedar": 'permit(principal == Borrower::"borrower", action, resource);'}   # everything, says the policy
    assert lend_mod.check(g, "read_file", lend_mod.targets(policy, "read_file", {"path": str(docs / "a.txt")})[0])[0]
    priv = lend_mod.targets(policy, "read_file", {"path": str(docs / "Private" / "x.txt")})[0]
    assert lend_mod.check(g, "read_file", priv) == (False, "it's in a private folder, which is never lent")
    assert "isn't something a lent Lantern does" in lend_mod.check(g, "run_in_project", priv)[1]
    assert lend_mod.check({**g, "expires_at": time.time() - 1}, "read_file", priv)[1] == "the loan has ended"
    moves = lend_mod.targets(policy, "organize_files", {"moves": [{"source": str(docs / "a.txt"),
                                                                   "destination": str(docs / "B" / "a.txt")}]})
    assert [m["name"] for m in moves] == ["a.txt", "a.txt"] and moves[1]["folders"][-1] == "B"
    web = lend_mod.targets(policy, "fill_form", {"url": "https://portal.example/apply", "goal": "x"})
    assert web == [{"kind": "web", "host": "portal.example", "path": "", "folder": "", "folders": [], "name": "",
                    "ext": ""}]
