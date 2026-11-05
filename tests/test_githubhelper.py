"""T8: GitHub helper pack; packs can declare needed MCP servers.

Fake GitHub responses and FakeClient only: never GitHub or the Meta API from tests."""
import base64
import json
import threading
import time
from pathlib import Path

import pytest

from fakes import FakeClient
from lantern import doctor, repolearn
from lantern.agentpacks import PackError, validate
from lantern.config import Config
from lantern.core import Core
from lantern.mcp_client import MCPHub, expand_env_refs

ROOT = Path(__file__).resolve().parent.parent
SHA = "b" * 40
SECRET = "ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"


def github(meta=None, files=None, texts=None):
    """A fake GitHub GET: repo metadata, top-level files, {name: text}."""
    meta = {"default_branch": "main", "archived": False, "stargazers_count": 7,
            "license": {"spdx_id": "MIT"}, **(meta or {})}
    files = ["README.md", "server.json", *(files or [])]
    texts = {"README.md": "# github-mcp-server\nGitHub's official MCP server.",
             **(texts or {})}
    seen = []

    def get(url):
        seen.append(url)
        if "/contents/?ref=" in url:
            return json.dumps([{"name": n, "type": "file"} for n in files]).encode()
        if "/contents/" in url:
            name = url.split("/contents/", 1)[1].split("?ref=", 1)[0]
            if name not in texts:
                raise repolearn.RepoError("not found")
            return json.dumps({"type": "file",
                               "content": base64.b64encode(texts[name].encode()).decode()}).encode()
        if "/commits/" in url:
            return json.dumps({"sha": SHA}).encode()
        return json.dumps(meta).encode()

    get.seen = seen
    return get


def helper_pack(**over):
    data = json.loads((ROOT / "agents" / "github-helper.lantern-agent.json").read_text(encoding="utf-8"))
    data.update(over)
    return data


def make_core(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    root = tmp_path / "docs"
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.audit_path = str(tmp_path / "audit.db")
    cfg.zones_file = str(tmp_path / "zones.json")
    cfg.agents_dir = str(tmp_path / "agents")
    cfg.mcp_config = str(tmp_path / "mcp.json")
    core = Core(cfg, FakeClient([]), approval_timeout=30)
    core.toolbox.github_get = github()
    return core


def pending(core, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        found = core.bus.pending()
        if found:
            return found[0]
        time.sleep(0.01)
    raise AssertionError("no approval was requested")


def run_in_thread(fn):
    out = {}
    def target():
        try:
            out["result"] = fn()
        except Exception as e:  # noqa: BLE001 - the test asserts on it
            out["error"] = e
    t = threading.Thread(target=target)
    t.start()
    return t, out


# ---- needs validation -----------------------------------------------------------
def test_needs_absent_ok_and_example_pack_validates():
    data = helper_pack()
    pack = validate(data)
    assert [(n.mcp, n.repo) for n in pack.needs] == [("github", "github/github-mcp-server")]
    assert pack.as_dict()["needs"] == data["needs"]


def test_needs_rejects_bad_shapes():
    base = helper_pack()
    with pytest.raises(PackError, match="at most"):
        validate({**base, "needs": [{"mcp": "github", "repo": "github/github-mcp-server",
                                     "why": "x"}] * 5})
    with pytest.raises(PackError, match="bad needed server name"):
        validate({**base, "needs": [{"mcp": "Bad Name!", "repo": "github/github-mcp-server", "why": "x"}]})
    with pytest.raises(PackError, match="owner/name"):
        validate({**base, "needs": [{"mcp": "github", "repo": "not-a-repo", "why": "x"}]})
    with pytest.raises(PackError, match="exactly 'mcp', 'repo' and 'why'"):
        validate({**base, "needs": [{"mcp": "github", "repo": "github/github-mcp-server"}]})
    with pytest.raises(PackError, match="exactly 'mcp', 'repo' and 'why'"):
        validate({**base, "needs": [{"mcp": "github", "repo": "github/github-mcp-server",
                                     "why": "x", "extra": 1}]})
    with pytest.raises(PackError, match="twice"):
        validate({**base, "needs": [base["needs"][0], base["needs"][0]]})


def test_needs_refuses_secrets():
    base = helper_pack()
    need = dict(base["needs"][0], why="use it " + SECRET)
    with pytest.raises(PackError, match="secret"):
        validate({**base, "needs": [need]})


# ---- the example pack is read-only ----------------------------------------------
def test_example_pack_has_no_write_tools():
    pack = validate(helper_pack())
    assert pack.tools, "the helper must list the tools it may use"
    for tool in pack.tools:
        assert tool.startswith("github."), tool
        assert tool.split(".", 1)[1] in repolearn.GITHUB_READ_TOOLS, tool
    assert "write the token" not in json.dumps(pack.as_dict())
    assert "never change anything" in pack.instructions.lower()


# ---- ${GITHUB_TOKEN} expansion ----------------------------------------------------
def test_env_ref_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    assert expand_env_refs({"env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}}) == \
        {"env": {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRET}}
    assert expand_env_refs("a-${MISSING_VAR}-b") == "a--b"
    assert expand_env_refs(["${GITHUB_TOKEN}", 3]) == [SECRET, 3]
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {"github": repolearn.github_server_spec(SHA)}}))
    hub = MCPHub.from_file(path)
    try:
        assert hub.servers["github"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == SECRET
        assert "${GITHUB_TOKEN}" in path.read_text(encoding="utf-8")   # still a ref on disk
    finally:
        hub.close()


# ---- the add-it card on install ----------------------------------------------------
def test_install_missing_server_shows_add_card_and_adds(tmp_path, monkeypatch):
    core = make_core(tmp_path, monkeypatch)
    cards = []
    try:
        t, out = run_in_thread(lambda: core.pack_install(helper_pack()))
        a = pending(core)
        assert a.action == "install_agent"
        core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
        b = pending(core)
        assert b.action == "add_needed_server"
        assert "This agent needs the GitHub tool server: Add it?" in b.summary
        assert SHA in b.summary and "MIT" in b.summary
        cards = [a.summary, b.summary]
        core.bus.decide(b.id, b.nonce, True, by="test", expected_hash=b.payload_hash)
        t.join(timeout=10)
        assert out["result"]["installed"] is True
        assert out["result"]["needed_servers"] == {"added": ["github"], "declined": [], "failed": {}}
        stored = json.loads(Path(core.toolbox.mcp_config).read_text(encoding="utf-8"))
        entry = stored["servers"]["github"]
        assert entry["read_only"] == [] and entry["source"] == {"repo": repolearn.GITHUB_MCP_REPO, "sha": SHA}
        assert entry["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
        assert "--read-only" in entry["args"]
        on_disk = Path(core.toolbox.mcp_config).read_text(encoding="utf-8")
        assert SECRET not in on_disk
        for card in cards:
            assert SECRET not in card
    finally:
        core.shutdown()


def test_install_decline_adds_nothing_but_keeps_pack(tmp_path, monkeypatch):
    core = make_core(tmp_path, monkeypatch)
    try:
        t, out = run_in_thread(lambda: core.pack_install(helper_pack()))
        a = pending(core)
        core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
        b = pending(core)
        assert b.action == "add_needed_server"
        core.bus.decide(b.id, b.nonce, False, by="test")
        t.join(timeout=10)
        assert out["result"]["installed"] is True   # the pack itself installed
        assert out["result"]["needed_servers"]["declined"] == ["github"]
        assert not Path(core.toolbox.mcp_config).exists()
    finally:
        core.shutdown()


def test_run_decline_runs_nothing(tmp_path, monkeypatch):
    core = make_core(tmp_path, monkeypatch)
    try:
        core.packs.install(helper_pack())
        before = [t for t in core.memory.tasks_with_status("active")]
        t, out = run_in_thread(lambda: core.submit_pack("github-helper",
                                                        {"repo": "o/r", "question": "summarize my open issues"}))
        b = pending(core)
        assert "Add it?" in b.summary
        core.bus.decide(b.id, b.nonce, False, by="test")
        t.join(timeout=10)
        assert "error" in out and "nothing was started" in str(out["error"])
        assert [x.id for x in core.memory.tasks_with_status("active")] == [x.id for x in before]
        assert not Path(core.toolbox.mcp_config).exists()
    finally:
        core.shutdown()


def test_needs_unknown_server_fails_without_asking(tmp_path, monkeypatch):
    core = make_core(tmp_path, monkeypatch)
    try:
        data = helper_pack(needs=[{"mcp": "bogus", "repo": "o/r", "why": "no such thing"}])
        core.packs.install(data)
        with pytest.raises(ValueError, match="needs bogus"):
            core.submit_pack("github-helper", {"repo": "o/r", "question": "hi"})
        assert core.bus.pending() == []   # no card for a server Lantern can't add
    finally:
        core.shutdown()


def test_token_never_in_pack_export_or_memory(tmp_path, monkeypatch):
    core = make_core(tmp_path, monkeypatch)
    try:
        t, out = run_in_thread(lambda: core.pack_install(helper_pack()))
        a = pending(core)
        core.bus.decide(a.id, a.nonce, True, by="test", expected_hash=a.payload_hash)
        b = pending(core)
        core.bus.decide(b.id, b.nonce, True, by="test", expected_hash=b.payload_hash)
        t.join(timeout=10)
        assert out["result"]["installed"] is True
        assert SECRET not in core.packs.export("github-helper").decode()
        assert SECRET not in Path(core.toolbox.mcp_config).read_text(encoding="utf-8")
        for task in core.memory.tasks_with_status("active") + core.memory.tasks_with_status("done"):
            for step in core.memory.steps(task.id):
                assert SECRET not in json.dumps(step)
    finally:
        core.shutdown()


# ---- doctor -----------------------------------------------------------------------
def test_doctor_github_token_set_and_unset(tmp_path):
    root = tmp_path / "docs"
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config(roots=[str(root)], db_path=str(tmp_path / "m.db"))
    cfg.zones_file = str(tmp_path / "zones.json")
    (tmp_path / "zones.json").write_text(json.dumps({"zones": []}))
    by_id = lambda checks: {c.id: c for c in checks}  # noqa: E731
    env = doctor.Env({"GITHUB_TOKEN": SECRET})
    assert by_id(doctor.run_checks(cfg, env))["github"].status == "ok"
    env = doctor.Env({})
    missing = by_id(doctor.run_checks(cfg, env))["github"]
    assert missing.status == "warn" and "not set" in missing.detail
    assert SECRET not in doctor.report(doctor.run_checks(cfg, doctor.Env({"GITHUB_TOKEN": SECRET})))
