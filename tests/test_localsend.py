"""T5: receive files and links from phones over home Wi-Fi (all offline)."""
import hashlib
import json

import pytest

from lantern import localsend as L
from lantern.approvals import CallbackGate
from lantern.audit import AuditLog
from lantern.policy import Policy


def kit(tmp_path, answer=True, pin=None, roots=None):
    roots = roots if roots is not None else [tmp_path]
    policy = Policy.from_roots(roots)
    inbox = tmp_path / "inbox"
    audit = AuditLog(tmp_path / "a.db")
    asked = []
    links = []

    def decide(summary):
        asked.append(summary)
        return answer(summary) if callable(answer) else answer

    rec = L.Receiver(policy, CallbackGate(decide), audit=audit,
                     on_link=lambda url: links.append(url) or 7,
                     home=tmp_path / "lan", inbox=inbox, pin=pin)
    return rec, policy, audit, asked, links


def offer(name, size, ftype="image/jpeg", sha=None):
    f = {"id": name, "fileName": name, "size": size, "fileType": ftype}
    if sha:
        f["sha256"] = sha
    return f


def body(files, alias="Pixel 8"):
    return {"info": {"alias": alias, "version": "2.0", "deviceType": "mobile",
                     "fingerprint": "peer-fp", "port": 53317, "protocol": "https"},
            "files": files}


# ---- info / register -------------------------------------------------------
def test_info_and_register_have_the_spec_fields(tmp_path):
    rec, *_ = kit(tmp_path)
    info = rec.info()
    assert info["alias"].startswith("Lantern on ") and info["alias"] == rec.alias
    assert info["version"] == "2.0" and info["deviceType"] == "desktop"
    assert info["protocol"] == "https" and info["download"] is False
    assert len(info["fingerprint"]) == 64 and info["fingerprint"].isupper()
    assert info["port"] == 53317
    back = rec.register({"alias": "Pixel 8", "fingerprint": "peer-fp"})
    assert back["alias"] == rec.alias and rec.peers == {"peer-fp": "Pixel 8"}


def test_identity_is_made_once_and_kept(tmp_path):
    rec, *_ = kit(tmp_path)
    again = L.ensure_identity(tmp_path / "lan" / "localsend", rec.alias)
    assert again.fingerprint == rec.fingerprint
    assert (tmp_path / "lan" / "localsend" / "cert.pem").is_file()


# ---- prepare: approval -------------------------------------------------------
def test_prepare_accepted_issues_tokens(tmp_path):
    rec, _, _, asked, _ = kit(tmp_path)
    code, resp = rec.prepare(body({"f1": offer("photo.jpg", 100), "f2": offer("notes.pdf", 50)}))
    assert code == 200 and resp["sessionId"] and len(resp["files"]) == 2
    t1, t2 = resp["files"]["f1"], resp["files"]["f2"]
    assert t1 != t2 and len(t1) >= 32
    assert len(asked) == 1 and "Pixel 8" in asked[0] and "photo.jpg" in asked[0]


def test_prepare_declined_rejects_and_writes_nothing(tmp_path):
    rec, _, audit, _, _ = kit(tmp_path, answer=False)
    code, resp = rec.prepare(body({"f1": offer("photo.jpg", 100)}))
    assert code == 403
    assert rec._session is None and not (tmp_path / "inbox").exists()
    rows = audit.entries()
    assert len(rows) == 1 and "declined" in rows[0]["preview"] and "photo.jpg" in rows[0]["preview"]


def test_second_prepare_while_pending_is_rejected(tmp_path):
    rec, *_ = kit(tmp_path)
    assert rec.prepare(body({"f1": offer("a.jpg", 10)}))[0] == 200
    assert rec.prepare(body({"f1": offer("b.jpg", 10)}))[0] == 409


def test_cancel_clears_the_session(tmp_path):
    rec, *_ = kit(tmp_path)
    _, resp = rec.prepare(body({"f1": offer("a.jpg", 10)}))
    assert rec.cancel(resp["sessionId"]) == (200, {})
    assert rec._session is None
    assert rec.prepare(body({"f1": offer("b.jpg", 10)}))[0] == 200  # free again


# ---- upload -------------------------------------------------------------------
def test_upload_flow_saves_and_verifies(tmp_path):
    rec, *_ = kit(tmp_path)
    _, resp = rec.prepare(body({"f1": offer("photo.jpg", 11)}))
    sid = resp["sessionId"]
    assert rec.upload(sid, "f1", resp["files"]["f1"], b"hello world") == (
        200, {"saved": "photo.jpg", "verified": True})
    assert (tmp_path / "inbox" / "photo.jpg").read_bytes() == b"hello world"
    assert rec._session is None  # all files arrived: the session is done


def test_upload_with_wrong_token_unknown_session_or_size_is_refused(tmp_path):
    rec, *_ = kit(tmp_path)
    _, resp = rec.prepare(body({"f1": offer("photo.jpg", 11)}))
    sid = resp["sessionId"]
    assert rec.upload("nope", "f1", resp["files"]["f1"], b"hello world")[0] == 403
    assert rec.upload(sid, "nope", resp["files"]["f1"], b"hello world")[0] == 403
    assert rec.upload(sid, "f1", "nope", b"hello world")[0] == 403
    assert rec.upload(sid, "f1", resp["files"]["f1"], b"too short")[0] == 403
    assert rec.upload(sid, "f1", "nope", b"too short")[0] == 403
    inbox = tmp_path / "inbox"
    assert not inbox.exists() or list(inbox.iterdir()) == []
    assert rec._session is not None  # nothing valid arrived: still pending


def test_upload_checks_the_announced_sha256(tmp_path):
    rec, *_ = kit(tmp_path)
    data = b"hello world"
    good = hashlib.sha256(data).hexdigest()
    _, resp = rec.prepare(body({"f1": offer("photo.jpg", len(data), sha=good)}))
    assert rec.upload(resp["sessionId"], "f1", resp["files"]["f1"], data)[0] == 200
    rec2, *_ = kit(tmp_path)
    _, resp2 = rec2.prepare(body({"f1": offer("photo.jpg", len(data), sha="0" * 64)}))
    assert rec2.upload(resp2["sessionId"], "f1", resp2["files"]["f1"], data)[0] == 422


# ---- names ----------------------------------------------------------------------
def test_name_sanitizing_and_dedup(tmp_path):
    rec, *_ = kit(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "photo.jpg").write_bytes(b"x")
    files = {"a": offer("../../evil", 1), "b": offer(".hidden", 1),
             "c": offer("x" * 150 + ".txt", 1), "d": offer("photo.jpg", 1)}
    _, resp = rec.prepare(body(files))
    sid = resp["sessionId"]
    for fid in ("a", "b", "c", "d"):
        assert rec.upload(sid, fid, resp["files"][fid], b"z")[0] == 200
    names = sorted(p.name for p in inbox.iterdir())
    assert "evil" in names and "hidden" in names and "photo (2).jpg" in names
    assert all(len(n) <= L.MAX_NAME_CHARS for n in names) and ".." not in "".join(names)


def test_inbox_outside_writable_zones_refuses_to_start(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    rec, *_ = kit(tmp_path, roots=[allowed])
    rec.inbox = tmp_path / "elsewhere" / "inbox"
    with pytest.raises(RuntimeError):
        rec.check_ready()
    server = L.Server(rec, port=53317)
    with pytest.raises(RuntimeError):
        server.start()
    assert server.listening is False


# ---- text and links ----------------------------------------------------------------
def test_a_link_message_submits_the_t1_goal(tmp_path):
    rec, _, _, _, links = kit(tmp_path)
    url = "https://youtu.be/abc?si=x"
    data = url.encode()
    _, resp = rec.prepare(body({"m1": offer("link", len(data), ftype="text/plain")}))
    assert rec.upload(resp["sessionId"], "m1", resp["files"]["m1"], data)[0] == 200
    assert links == [url]  # on_link got the exact URL for the T1 goal
    inbox = tmp_path / "inbox"
    assert not inbox.exists() or list(inbox.iterdir()) == []  # links never touch the inbox


def test_receive_text_routes_links_and_keeps_notes(tmp_path):
    rec, _, _, _, links = kit(tmp_path)
    out = rec.receive_text("Pixel 8", "Cool video https://youtu.be/abc?si=x")
    assert out["kind"] == "link" and links == ["https://youtu.be/abc?si=x"]
    assert out["goal"] == "[localsend] open this link on my laptop: https://youtu.be/abc?si=x"
    out = rec.receive_text("Pixel 8", "buy milk after work")
    assert out["kind"] == "text" and rec.texts == [{"sender": "Pixel 8", "text": "buy milk after work"}]
    assert not (tmp_path / "inbox").exists()


# ---- PIN ------------------------------------------------------------------------------
def test_pin_is_required_when_set(tmp_path):
    rec, *_ = kit(tmp_path, pin="1234")
    files = {"f1": offer("a.jpg", 5)}
    assert rec.prepare(body(files))[0] == 401
    assert rec.prepare(body(files), pin="0000")[0] == 401
    assert rec.prepare(body(files), pin="1234")[0] == 200
    plain, *_ = kit(tmp_path)
    assert plain.prepare(body(files))[0] == 200  # no PIN set: no PIN asked


# ---- off by default -------------------------------------------------------------------
def test_off_by_default(tmp_path):
    from lantern.config import Config
    assert L.enabled_in({}) is False
    assert L.enabled_in({"LANTERN_LOCALSEND": "1"}) is True
    assert L.enabled_in({"LANTERN_LOCALSEND": "0"}, Config(localsend=True)) is False
    assert L.enabled_in({}, Config(localsend=True)) is True
    rec, *_ = kit(tmp_path)
    server = L.Server(rec, port=53317)
    assert server.listening is False  # constructed but never started: nothing bound


# ---- audit -------------------------------------------------------------------------------
def test_audit_row_has_names_and_sizes_but_no_file_bytes(tmp_path):
    rec, _, audit, _, _ = kit(tmp_path, answer=True)
    data = b"super secret file bytes 12345"
    _, resp = rec.prepare(body({"f1": offer("photo.jpg", len(data))}, alias="Pixel 8"))
    rec.upload(resp["sessionId"], "f1", resp["files"]["f1"], data)
    rows = audit.entries()
    assert rows, "the transfer must be audited"
    dumped = json.dumps(rows)
    assert data.decode() not in dumped and data.hex() not in dumped
    assert "Pixel 8" in dumped and "photo.jpg" in dumped and str(len(data)) in dumped
    assert all(r["lane"] == "local" for r in rows)  # nothing left the laptop
