"""M10: the desktop app's logic tests (node --test) run as part of the Python suite."""
import shutil
import subprocess
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parent.parent / "desktop"


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
def test_desktop_logic_and_mock_server():
    r = subprocess.run(["node", "--test", "test/**/*.test.js"], cwd=DESKTOP, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "# fail 0" in r.stdout


def test_renderers_are_locked_down():
    main = (DESKTOP / "main.js").read_text()
    for flag in ("contextIsolation: true", "nodeIntegration: false", "sandbox: true", "setWindowOpenHandler",
                 "setPermissionRequestHandler", "setIgnoreMouseEvents(true, { forward: true })"):
        assert flag in main, flag
    for page in ("panel.html", "avatar.html", "quick.html"):
        html = (DESKTOP / "renderer" / page).read_text()
        assert "Content-Security-Policy" in html and "script-src 'self'" in html and "unsafe-eval" not in html
    host = (DESKTOP / "renderer" / "widget-host.html").read_text()
    assert "default-src 'none'" in host and "connect-src" not in host
    panel = (DESKTOP / "renderer" / "panel.html").read_text()
    assert 'sandbox="allow-scripts"' in panel and "allow-same-origin" not in panel
    for script in ("panel.js", "quick.js"):   # server text goes in as text
        assert ".innerHTML" not in (DESKTOP / "renderer" / script).read_text(), script


def test_tray_icons_are_ours_and_up_to_date():
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_tray_icons", DESKTOP.parent / "scripts" / "make_tray_icons.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, rgb in (("lanternTemplate", (0, 0, 0)), ("lantern", (255, 159, 10))):
        for suffix, size in (("", 16), ("@2x", 32)):
            committed = (DESKTOP / "assets" / "tray" / f"{name}{suffix}.png").read_bytes()
            assert committed.startswith(b"\x89PNG") and committed == mod.render(size, rgb), f"{name}{suffix}"
