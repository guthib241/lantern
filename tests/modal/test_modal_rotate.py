"""deploy/modal/rotate_token.py with a fake Modal module: no network, token never printed."""
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "modal"))
import glimmer_config as gc  # noqa: E402
import rotate_token as rt  # noqa: E402


class NotFoundError(Exception):
    pass


class FakeModal:
    """Just enough of `modal` for rotate_token: Secret.from_name/hydrate/update and Secret.objects.create."""

    def __init__(self, exists=False, fail=False):
        self.store: dict[str, dict] = {gc.SECRET_NAME: {gc.TOKEN_KEY: "old"}} if exists else {}
        self.fail = fail
        fake = self

        class exception:
            NotFoundError = globals()["NotFoundError"]

        class _Objects:
            def create(self, name, env_dict):
                if fake.fail:
                    raise RuntimeError("network down")
                fake.store[name] = dict(env_dict)

        class Secret:
            objects = _Objects()

            def __init__(self, name):
                self.name = name

            @classmethod
            def from_name(cls, name):
                return cls(name)

            def hydrate(self):
                if self.name not in fake.store:
                    raise NotFoundError(self.name)

            def update(self, env_dict):
                if fake.fail:
                    raise RuntimeError("network down")
                fake.store[self.name].update(env_dict)

        self.exception = exception
        self.Secret = Secret


def test_first_run_creates_the_secret_and_a_private_file(tmp_path):
    modal = FakeModal()
    path = tmp_path / "lantern" / "private_token"
    assert rt.rotate(path, modal) == "created"
    token = path.read_text().strip()
    assert gc.validate_token(token) and len(token) == 43
    assert modal.store[gc.SECRET_NAME] == {gc.TOKEN_KEY: token}
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not path.with_name("private_token.new").exists()


def test_rotation_updates_the_secret_and_replaces_the_file(tmp_path):
    modal = FakeModal(exists=True)
    path = tmp_path / "private_token"
    path.write_text("old\n")
    assert rt.rotate(path, modal) == "updated"
    new = path.read_text().strip()
    assert new != "old" and modal.store[gc.SECRET_NAME][gc.TOKEN_KEY] == new


def test_failed_modal_call_keeps_the_old_token(tmp_path):
    path = tmp_path / "private_token"
    path.write_text("old\n")
    with pytest.raises(RuntimeError):
        rt.rotate(path, FakeModal(exists=True, fail=True))
    assert path.read_text() == "old\n"
    assert not path.with_name("private_token.new").exists()


def test_tokens_are_fresh_each_time():
    assert len({rt.new_token() for _ in range(50)}) == 50


def test_main_never_prints_the_token(tmp_path, monkeypatch, capsys):
    modal = FakeModal()
    real_push = rt.push_secret
    monkeypatch.setattr(rt, "push_secret", lambda token, modal_module=None: real_push(token, modal))
    path = tmp_path / "tok"
    assert rt.main(["--token-file", str(path)]) == 0
    out = capsys.readouterr()
    token = path.read_text().strip()
    assert modal.store[gc.SECRET_NAME][gc.TOKEN_KEY] == token
    assert token not in out.out and token not in out.err
    assert "saved to" in out.out
