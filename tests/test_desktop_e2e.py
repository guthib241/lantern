"""End to end: the real Electron app against the real server in rehearsal mode (desktop/e2e/run.js).

Runs when Electron is installed (cd desktop && npm install) and there's a display or Xvfb; skips otherwise."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parent.parent / "desktop"
ELECTRON = DESKTOP / "node_modules" / "electron" / "dist"
PLAYWRIGHT = DESKTOP / "node_modules" / "playwright"
HEADLESS = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")


@pytest.mark.skipif(not shutil.which("node") or not ELECTRON.exists() or not PLAYWRIGHT.exists(),
                    reason="desktop dependencies not installed (cd desktop && npm install)")
@pytest.mark.skipif(HEADLESS and not shutil.which("xvfb-run"), reason="no display and no Xvfb")
def test_desktop_app_end_to_end(tmp_path):
    cmd = ["node", "e2e/run.js"]
    if HEADLESS:
        cmd = ["xvfb-run", "-a"] + cmd
    env = {**os.environ, "PYTHON": sys.executable, "E2E_OUT": str(tmp_path)}
    r = subprocess.run(cmd, cwd=DESKTOP, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-3000:]
    assert "all end-to-end checks passed" in r.stdout
    assert {p.name for p in tmp_path.glob("*.png")} >= {"1-form-verified.png", "2-privacy.png", "6-setup.png"}
