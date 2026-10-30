"""tests/freecad: the FreeCAD connector (S17). Shares tests/fakes.py with the main suite."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # tests/, for `from fakes import FakeClient`

import pytest


@pytest.fixture(autouse=True)
def quick_renders(request, monkeypatch):
    """Voxel measuring and rendering after every change are the slow part of the fake; tests that look at
    pixels or volumes keep full resolution (@pytest.mark.full_render)."""
    if "full_render" not in request.keywords:
        from lantern.integrations.freecad import fake
        monkeypatch.setattr(fake, "RENDER_GRID", 16)
        monkeypatch.setattr(fake, "GRID", 24)


def pytest_configure(config):
    config.addinivalue_line("markers", "full_render: keep the fake backend's full voxel resolution")
