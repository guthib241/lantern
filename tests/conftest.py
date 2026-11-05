"""Shared test setup: keys for encrypted memory come from a file in the test folder,
never from the developer's real OS keychain; and no test leaves environment variables behind."""
import os

import pytest


@pytest.fixture(autouse=True)
def _file_key_store(monkeypatch):
    monkeypatch.setenv("LANTERN_KEY_STORE", "file")


@pytest.fixture(autouse=True)
def _restore_environ():
    """Some scripts set process-wide variables when run (eval_privacy.py and phase2_live.py set
    LANTERN_TASK_BUDGET_USD=0.25 for their own run). Called in-process by a test, that value outlived the
    test and changed a later one's scheduled-run budget (seen when the branches were combined). Every test now
    starts and ends with the same environment."""
    saved = dict(os.environ)
    yield
    for key in set(os.environ) - set(saved):
        del os.environ[key]
    for key, value in saved.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
