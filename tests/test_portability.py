"""The demo laptop runs Windows. There, text files and subprocess output default to cp1252, which crashes on
₹, → or emoji and garbles UTF-8. Every text-mode open says encoding= explicitly, and the policy's own
read/write round-trip byte-exactly, so "verified" means the same on every OS."""
import ast
from pathlib import Path

from lantern.memory import Memory
from lantern.policy import Policy
from lantern.tools import Toolbox

REPO = Path(__file__).resolve().parent.parent
SOURCES = sorted([*REPO.glob("lantern/**/*.py"), *REPO.glob("scripts/*.py")])


def _kw(node: ast.Call, name: str):
    return next((k.value for k in node.keywords if k.arg == name), None)


def _text_mode(mode) -> bool:
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" not in mode.value


def _receiver(node: ast.Call) -> str:
    if not isinstance(node.func, ast.Attribute):
        return ""
    v = node.func.value
    return v.attr if isinstance(v, ast.Attribute) else v.id if isinstance(v, ast.Name) else ""


def unencoded_text_io(source: str) -> list[int]:
    """Line numbers of text-mode file or subprocess I/O that leaves the encoding to the OS."""
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or _kw(node, "encoding") is not None:
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
        if name in ("read_text", "write_text") and isinstance(f, ast.Attribute) and _receiver(node) != "policy":
            bad.append(node.lineno)        # Path.read_text / write_text (Policy's own methods are fine)
        elif name == "open" and isinstance(f, ast.Name):
            mode = node.args[1] if len(node.args) > 1 else _kw(node, "mode")
            if mode is None or _text_mode(mode):
                bad.append(node.lineno)    # builtin open(path) or open(path, "w")
        elif name in ("open", "fdopen") and isinstance(f, ast.Attribute) and _receiver(node) not in ("wave", "webbrowser"):
            mode = node.args[0] if name == "open" and node.args else node.args[1] if len(node.args) > 1 else _kw(node, "mode")
            if _text_mode(mode):
                bad.append(node.lineno)    # Path.open("r") / os.fdopen(fd, "w")
        elif name in ("run", "check_output", "Popen") and _receiver(node) == "subprocess":
            text = _kw(node, "text") or _kw(node, "universal_newlines")
            if isinstance(text, ast.Constant) and text.value is True:
                bad.append(node.lineno)    # decoded with the OS code page
    return bad


def test_every_text_open_names_its_encoding():
    offenders = {str(p.relative_to(REPO)): lines for p in SOURCES
                 if (lines := unencoded_text_io(p.read_text(encoding="utf-8")))}
    assert offenders == {}


def test_the_checker_catches_what_it_should():
    assert unencoded_text_io("from pathlib import Path\nPath('x').read_text()\n") == [2]
    assert unencoded_text_io("open('x')\nopen('x', 'rb')\nopen('x', 'w', encoding='utf-8')\n") == [1]
    assert unencoded_text_io("import subprocess\nsubprocess.run(['a'], text=True)\n") == [2]
    assert unencoded_text_io("import os\nos.fdopen(3, 'w')\nself.policy.read_text(p)\np.open('r+b')\n") == [2]


def test_files_lantern_writes_read_back_exactly(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    mem = Memory(tmp_path / "m.db")
    tb = Toolbox([root], mem, lambda summary: True)
    content = "Total: ₹2,400 → paid ✓ 🏮\r\nwindows line\nunix line\rold mac line\n"
    r = tb.write_file("notes.txt", content)
    assert r["verified"] is True
    assert (root / "notes.txt").read_bytes() == content.encode("utf-8")   # no newline translation on any OS
    assert Policy.from_roots([root]).read_text(root / "notes.txt") == content


def test_dotenv_saved_by_powershell_or_notepad_still_loads(tmp_path, monkeypatch):
    import os
    from lantern.config import load_env, read_env_file
    line = "MODEL_API_KEY=test-key-not-real\nLANTERN_PERSONA=helpful\n"
    for name, data in {"utf16": line.encode("utf-16"),            # Windows PowerShell 5.1: echo ... > .env
                       "utf8bom": b"\xef\xbb\xbf" + line.encode(),  # Notepad's "UTF-8 with BOM"
                       "utf8": line.encode()}.items():
        f = tmp_path / f"{name}.env"
        f.write_bytes(data)
        assert read_env_file(f) == line, name
        monkeypatch.delenv("MODEL_API_KEY", raising=False)
        monkeypatch.delenv("LANTERN_PERSONA", raising=False)
        assert load_env(f) is True
        assert os.environ["MODEL_API_KEY"] == "test-key-not-real" and os.environ["LANTERN_PERSONA"] == "helpful", name
    monkeypatch.setenv("MODEL_API_KEY", "already-set")
    load_env(tmp_path / "utf8.env")
    assert os.environ["MODEL_API_KEY"] == "already-set"             # the real environment wins
    assert load_env(tmp_path / "missing.env") is False
