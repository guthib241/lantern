"""M2: privacy zones. Hidden never listed, private never read for the cloud, writes only in writable."""
import json

import pytest

from fakes import FakeClient, call
from lantern.agent import Agent
from lantern.memory import Memory
from lantern.policy import HIDDEN, OPEN, PRIVATE, WRITABLE, Policy, PolicyError, Rule
from lantern.tools import Toolbox, ToolError


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    docs = home / "Documents"
    priv = home / "Private"
    downloads = home / "Downloads"
    secret = docs / "vault"
    for d in (docs, priv, downloads, secret, home / ".ssh"):
        d.mkdir(parents=True)
    (docs / "plan.txt").write_text("project plan for the hackathon")
    (priv / "offer_letter.txt").write_text("Salary: 42 lakh\nStart: Dec 1\n")
    (downloads / "invoice.txt").write_text("invoice 99")
    (secret / "pins.txt").write_text("1234")
    (home / ".ssh" / "id_rsa").write_text("KEY")
    (docs / ".env").write_text("MODEL_API_KEY=abc")
    policy = Policy([
        Rule.make(docs, WRITABLE), Rule.make(priv, PRIVATE),
        Rule.make(downloads, OPEN), Rule.make(secret, HIDDEN),
        Rule.make(home, OPEN),
    ])
    mem = Memory(tmp_path / "m.db")
    return home, policy, mem


def test_longest_match_wins(world):
    home, policy, _ = world
    assert policy.zone_of(home / "Documents" / "plan.txt") == WRITABLE
    assert policy.zone_of(home / "Documents" / "vault" / "pins.txt") == HIDDEN
    assert policy.zone_of(home / "Private" / "offer_letter.txt") == PRIVATE
    assert policy.zone_of(home / "Downloads") == OPEN
    assert policy.zone_of(home.parent / "elsewhere") == HIDDEN  # outside every rule


def test_always_hidden_names_beat_rules(world):
    home, policy, _ = world
    assert policy.zone_of(home / ".ssh" / "id_rsa") == HIDDEN
    assert policy.zone_of(home / "Documents" / ".env") == HIDDEN


def test_hidden_paths_never_listed_or_searched(world):
    home, policy, mem = world
    tb = Toolbox(policy, mem, lambda d: True)
    names = [e["name"] for e in tb.list_dir(str(home / "Documents"))["entries"]]
    assert "plan.txt" in names and "vault/" not in names
    found = json.dumps(tb.search_files("*"))
    assert "pins.txt" not in found and "id_rsa" not in found and ".env" not in found
    assert "vault" not in json.dumps(tb.list_folders())


def test_hidden_error_looks_like_outside(world, tmp_path):
    home, policy, mem = world
    tb = Toolbox(policy, mem, lambda d: True)
    with pytest.raises(ToolError) as hidden:
        tb.read_file(str(home / "Documents" / "vault" / "pins.txt"))
    with pytest.raises(ToolError) as outside:
        tb.read_file("/etc/passwd")
    assert "outside the folders" in str(hidden.value) and "outside the folders" in str(outside.value)


def test_private_file_described_not_read(world):
    home, policy, mem = world
    tb = Toolbox(policy, mem, lambda d: True)
    r = tb.read_file(str(home / "Private" / "offer_letter.txt"))
    assert r["private"] and "42 lakh" not in json.dumps(r)
    assert r["description"]["name"] == "offer_letter.txt" and r["description"]["lines"] == 2
    with pytest.raises(PolicyError):
        policy.read_text(home / "Private" / "offer_letter.txt")
    assert "42 lakh" in policy.read_text(home / "Private" / "offer_letter.txt", allow_private=True)


def test_private_content_not_searched(world):
    home, policy, mem = world
    tb = Toolbox(policy, mem, lambda d: True)
    assert tb.search_files("lakh", in_content=True)["count"] == 0
    assert tb.search_files("offer")["count"] == 1  # names are visible


def test_writes_only_in_writable_zones(world):
    home, policy, mem = world
    tb = Toolbox(policy, mem, lambda d: True)
    assert tb.write_file(str(home / "Documents" / "new.txt"), "hi")["verified"]
    for target in (home / "Downloads" / "x.txt", home / "Private" / "x.txt"):
        with pytest.raises(ToolError, match="writable"):
            tb.write_file(str(target), "nope")
    with pytest.raises(ToolError):
        tb.move_file(str(home / "Downloads" / "invoice.txt"), str(home / "Documents"))
    assert (home / "Downloads" / "invoice.txt").exists()


def test_symlink_into_hidden_is_hidden(world):
    home, policy, mem = world
    (home / "Documents" / "sneaky.txt").symlink_to(home / "Documents" / "vault" / "pins.txt")
    tb = Toolbox(policy, mem, lambda d: True)
    with pytest.raises(ToolError):
        tb.read_file(str(home / "Documents" / "sneaky.txt"))
    assert "sneaky" not in json.dumps(tb.list_dir(str(home / "Documents")))


def test_sensitive_names_opt_in(tmp_path):
    (tmp_path / "Bank Statements").mkdir()
    off = Policy([Rule.make(tmp_path, OPEN)])
    on = Policy([Rule.make(tmp_path, OPEN)], hide_sensitive_names=True)
    assert off.zone_of(tmp_path / "Bank Statements") == OPEN
    assert on.zone_of(tmp_path / "Bank Statements" / "a.pdf") == HIDDEN


def test_zones_json_round_trip(world, tmp_path):
    home, policy, _ = world
    zf = tmp_path / "zones.json"
    policy.set_zone(home / "Downloads", PRIVATE)
    policy.save(zf)
    loaded = Policy.load(zf, roots=[])
    assert loaded.zone_of(home / "Downloads" / "invoice.txt") == PRIVATE
    assert loaded.zone_of(home / "Documents" / "vault") == HIDDEN


def test_missing_zones_json_uses_roots_as_writable(tmp_path):
    p = Policy.load(tmp_path / "nope.json", roots=[tmp_path])
    assert p.zone_of(tmp_path / "a.txt") == WRITABLE


def test_bad_zone_rejected(tmp_path):
    with pytest.raises(ValueError):
        Rule.make(tmp_path, "secret")
    with pytest.raises(ValueError):
        Policy([Rule.make(tmp_path, HIDDEN)])


def test_agent_never_sees_private_text(world):
    home, policy, mem = world
    tb = Toolbox(policy, mem, lambda d: True)
    client = FakeClient([
        ([call("read_file", "c1", path=str(home / "Private" / "offer_letter.txt"))], ""),
        ([], "It's private, so I only know its size."),
    ])
    Agent(client, mem, tb).run("what's my salary?")
    sent = json.dumps(client.requests, default=str)
    assert "42 lakh" not in sent and "offer_letter.txt" in sent


def test_no_module_opens_user_files_directly():
    """Static guard: only policy.py does file I/O on user paths."""
    from pathlib import Path
    pkg = Path(__file__).parent.parent / "lantern"
    banned = ("os.walk(", ".iterdir(", "shutil.move(", "shutil.copy", ".unlink(", "os.remove(", "rmtree(")
    for f in pkg.rglob("*.py"):
        if f.name == "policy.py":
            continue
        text = f.read_text()
        for b in banned:
            assert b not in text, f"{f.name} uses {b}; go through policy.py"
