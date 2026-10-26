"""Shared test setup: keys for encrypted memory come from a file in the test folder,
never from the developer's real OS keychain."""
import pytest


@pytest.fixture(autouse=True)
def _file_key_store(monkeypatch):
    monkeypatch.setenv("LANTERN_KEY_STORE", "file")
