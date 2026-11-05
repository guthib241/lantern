"""T6 learn_repo: read a GitHub repo, install pinned MCP servers with approval,
build wrappers with Muse Code, draft packs from docs. Fake GitHub responses and
FakeClient only; never GitHub or the Meta API from tests."""
import base64
import json

import pytest

from fakes import FakeClient
from lantern import repolearn
from lantern.approvals import CallbackGate
from lantern.memory import Memory
from lantern.tools import TOOL_NAMES, Toolbox, ToolError

SHA = "a" * 40
SUMMARY_PIP = {"kind": "mcp_server", "explanation": "A weather MCP server.",
               "tools": ["get_forecast"], "install_method": "pip", "risks": ["network access"]}
PYPROJECT_MCP = '[project]\nname = "weather-mcp"\ndependencies = ["mcp>=1.0"]\n'
PYPROJECT_LIB = '[project]\nname = "shout"\ndependencies = ["requests"]\n'
PACKAGE_MCP = json.dumps({"name": "w", "bin": {"w": "./bin/w.js"},
                          "dependencies": {"@modelcontextprotocol/sdk": "^1.0"}})


def github(meta=None, files=None, texts=None):
    """A fake GitHub GET: meta dict, top-level file names, {name: text}."""
    meta = {"default_branch": "main", "archived": False, "stargazers_count": 42,
            "license": {"spdx_id": "MIT"}, **(meta or {})}
    files = ["README.md", "pyproject.toml", *(files or [])]
    texts = {"README.md": "# Weather\nGet forecasts.", "pyproject.toml": PYPROJECT_MCP,
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


def summary_script(summary, extra=()):
    return [([], json.dumps(summary)), *extra]


class Runner:
    """Fake command_runner: records argv, replays (code, output)."""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def __call__(self, argv, cwd, env, timeout):
        self.calls.append(argv)
        return self.results.pop(0) if self.results else (0, "ok")


def make_tb(tmp_path, script, approve=True, github_get=None, runner=None, probe=None):
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    mem = Memory(tmp_path / "m.db")
    seen = []
    gate = CallbackGate(lambda s: seen.append(s) or approve)
    tb = Toolbox([docs], mem, gate)
    tb.client = FakeClient(script)
    tb.github_get = github_get or github()
    tb.mcp_config = str(tmp_path / "mcp.json")
    tb.command_runner = runner or Runner()
    tb.mcp_probe = probe or (lambda name, spec: ["get_forecast"])
    tid = mem.start_task("learn https://github.com/o/weather-mcp").id
    tb.begin_task(tid, "learn it")
    return tb, mem, seen


# ---- URL validation ----------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://gist.github.com/o/1234",
    "https://example.com/o/r",
    "javascript:alert(1)",
    "https://github.com/o/r/blob/main/x.py",
    "https://github.com/o",
    "not a url",
])
def test_bad_urls_refused(tmp_path, url):
    tb, _, _ = make_tb(tmp_path, [])
    with pytest.raises(ToolError):
        tb.learn_repo(url)


def test_tree_ref_pins_that_ref(tmp_path):
    get = github()
    tb, _, _ = make_tb(tmp_path, summary_script(SUMMARY_PIP), github_get=get)
    out = tb.learn_repo("https://github.com/o/weather-mcp/tree/v1.2.3")
    assert out["sha"] == SHA
    assert any(u.endswith("/commits/v1.2.3") for u in get.seen)


# ---- (A) install ---------------------------------------------------------------
def test_sha_pinned_in_approval_and_install_command(tmp_path):
    runner = Runner()
    tb, _, seen = make_tb(tmp_path, summary_script(SUMMARY_PIP), runner=runner)
    out = tb.learn_repo("https://github.com/o/weather-mcp")
    assert out["installed"] is True and out["verified"] is True
    assert SHA in seen[0] and "MIT" in seen[0] and "42" in seen[0]
    assert any(SHA in " ".join(a) for a in runner.calls)
    pip = [a for a in runner.calls if "pip" in a][0]
    assert pip[-1] == f"git+https://github.com/o/weather-mcp@{SHA}"


def test_no_license_refused_before_approval(tmp_path):
    get = github(meta={"license": None})
    tb, _, seen = make_tb(tmp_path, [], github_get=get)
    with pytest.raises(ToolError, match="license"):
        tb.learn_repo("https://github.com/o/weather-mcp")
    assert seen == [] and tb.client.requests == []


def test_npm_command_has_ignore_scripts(tmp_path):
    runner = Runner()
    summary = {**SUMMARY_PIP, "install_method": "npm"}
    get = github(files=["package.json"],
                 texts={"package.json": PACKAGE_MCP, "pyproject.toml": ""})
    tb, _, _ = make_tb(tmp_path, summary_script(summary), github_get=get, runner=runner)
    out = tb.learn_repo("https://github.com/o/weather-mcp")
    assert out["installed"] is True
    npm = [a for a in runner.calls if a[0] == "npm"][0]
    assert "--ignore-scripts" in npm and npm[-1] == "github:o/weather-mcp#" + SHA


def test_decline_installs_nothing(tmp_path):
    before = {"servers": {"keep": {"command": "x", "args": []}}}
    (tmp_path / "mcp.json").write_text(json.dumps(before))
    runner = Runner()
    tb, _, _ = make_tb(tmp_path, summary_script(SUMMARY_PIP), approve=False, runner=runner)
    out = tb.learn_repo("https://github.com/o/weather-mcp")
    assert out["installed"] is False and out["reason"] == "user declined"
    assert json.loads((tmp_path / "mcp.json").read_text()) == before
    assert runner.calls == []


def test_learned_server_is_read_only_with_source(tmp_path):
    tb, _, _ = make_tb(tmp_path, summary_script(SUMMARY_PIP))
    out = tb.learn_repo("https://github.com/o/weather-mcp")
    entry = json.loads((tmp_path / "mcp.json").read_text())["servers"]["weather-mcp"]
    assert entry["read_only"] == [] and entry["source"] == {"repo": "o/weather-mcp", "sha": SHA}
    assert out["server_tools"] == ["get_forecast"]


def test_failed_start_rolls_back_entry(tmp_path):
    (tmp_path / "mcp.json").write_text(json.dumps({"servers": {"keep": {"command": "x"}}}))

    def dead(name, spec):
        raise RuntimeError("connection refused")

    tb, _, _ = make_tb(tmp_path, summary_script(SUMMARY_PIP), probe=dead)
    out = tb.learn_repo("https://github.com/o/weather-mcp")
    assert out["installed"] is False and "removed it again" in out["reason"]
    servers = json.loads((tmp_path / "mcp.json").read_text())["servers"]
    assert "weather-mcp" not in servers and "keep" in servers


def test_readme_wrapped_as_untrusted_and_task_tainted(tmp_path):
    tb, _, _ = make_tb(tmp_path, summary_script(SUMMARY_PIP))
    out = tb.learn_repo("https://github.com/o/weather-mcp")
    assert out["readme"]["untrusted"] is True and "Get forecasts" in out["readme"]["content"]
    assert tb.tainted is True


def test_facts_win_over_the_model(tmp_path):
    summary = {"kind": "library_or_app", "explanation": "Just a lib.",
               "tools": [], "install_method": "none", "risks": []}
    tb, _, _ = make_tb(tmp_path, summary_script(summary), approve=False)
    out = tb.learn_repo("https://github.com/o/weather-mcp")
    assert out["kind"] == "mcp_server"   # pyproject depends on mcp, whatever the model said
    assert out["installed"] is False


# ---- (B) wrapper ------------------------------------------------------------------
def test_wrapper_skipped_without_muse(tmp_path, monkeypatch):
    monkeypatch.setattr(repolearn, "find_muse", lambda: None)
    summary = {"kind": "library_or_app", "explanation": "A lib.",
               "tools": ["shout"], "install_method": "none", "risks": []}
    get = github(texts={"pyproject.toml": PYPROJECT_LIB})
    tb, _, seen = make_tb(tmp_path, summary_script(summary), github_get=get)
    out = tb.learn_repo("https://github.com/o/shout")
    assert out["wrapper"] == "skipped" and "isn't installed" in out["reason"]
    assert seen == []   # no approval asked


def test_wrapper_command_flags_and_full_build(tmp_path, monkeypatch):
    monkeypatch.setattr(repolearn, "find_muse", lambda: "/usr/bin/muse")
    runner = Runner()
    summary = {"kind": "library_or_app", "explanation": "A lib.",
               "tools": ["shout"], "install_method": "none", "risks": ["needs a key"]}
    get = github(texts={"README.md": "# Shout\nShout text.", "pyproject.toml": PYPROJECT_LIB})
    tb, _, seen = make_tb(tmp_path, summary_script(summary), github_get=get, runner=runner)
    out = tb.learn_repo("https://github.com/o/shout")
    muse_calls = [a for a in runner.calls if a[0] == "/usr/bin/muse"]
    assert len(muse_calls) == 1 and muse_calls[0][1] == "exec"
    assert "--yolo" not in muse_calls[0] and "--contributor-mode" in muse_calls[0]
    assert muse_calls[0][muse_calls[0].index("--contributor-mode") + 1] == "off"
    assert "150" in muse_calls[0] and "needs a key" in seen[0]
    assert out["wrapper"] == "built" and out["installed"] is True
    entry = json.loads((tmp_path / "mcp.json").read_text())["servers"]["shout"]
    assert entry["source"]["wrapper"].endswith("builds/shout")


# ---- (C) docs ----------------------------------------------------------------------
def test_docs_draft_tools_are_lanterns_own(tmp_path):
    summary = {"kind": "docs", "explanation": "A how-to guide.",
               "tools": [], "install_method": "none", "risks": []}
    draft = {"name": "use-thing", "title": "Use thing", "description": "Does the thing.",
             "author": "dana", "version": "1.0.0",
             "inputs": [{"key": "target", "question": "What for?", "example": "demo"}],
             "goal": "Do the thing for {target}.",
             "instructions": "List folders, then read the file.",
             "tools": ["list_folders", "teleport"]}
    get = github(files=["GUIDE.md"], texts={"pyproject.toml": "", "package.json": ""})
    tb, _, _ = make_tb(tmp_path, summary_script(summary, [([], json.dumps(draft))]),
                       github_get=get)
    out = tb.learn_repo("https://github.com/o/guide")
    assert out["kind"] == "docs"
    assert set(out["pack_draft"]["tools"]) <= set(TOOL_NAMES) | {"finish"}
    assert out["pack_draft"]["tools"] == ["list_folders"]
