"""Receive files and links from phones over home Wi-Fi (LocalSend protocol).

Our own implementation of the public protocol spec
(https://github.com/localsend/protocol, README v2.x). No LocalSend code is
used: the shapes below (multicast group, v2 HTTPS paths, the prepare/upload/
cancel flow, the PIN query parameter) come from that README only.

Off by default: nothing listens until the receiver is started (Setup switch
or LANTERN_LOCALSEND=1). When on:

  - identity: alias ``Lantern on <hostname>``, deviceType ``desktop``, a
    self-signed TLS certificate made once and kept in
    ``~/.lantern/localsend/``; the fingerprint is the SHA-256 of the
    certificate, as the spec defines it;
  - discovery: listen on the spec's multicast group and port, answer
    announcements, announce ourselves on start (failures stay graceful);
  - HTTPS endpoints (v2 paths): info, register, prepare-upload, upload,
    cancel. prepare-upload never auto-accepts: it raises a Lantern approval
    (desktop, and the phone channel if one is connected). Accept issues
    per-file tokens; decline maps to the spec's reject;
  - saving goes only through ``lantern.policy`` into an inbox that must be a
    writable zone; names are sanitized and de-duplicated, sizes are enforced;
  - text shares (``text/plain``) never touch the inbox: a web link submits
    the T1 "open on my laptop" goal, other text is kept for the panel.

All received content is untrusted data, never instructions.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
import secrets
import socket
import socketserver
import ssl
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import linkopen
from .approvals import ApprovalError, Gate, as_gate
from .policy import PolicyError

# From the protocol README (v2.x). Everything on this page is configurable in
# the LocalSend app, so these are the defaults we speak.
MULTICAST_ADDR = "224.0.0.167"
MULTICAST_PORT = 53317
DEFAULT_PORT = 53317
PROTOCOL_VERSION = "2.0"

MAX_FILES = 100
MAX_TRANSFER_BYTES = 500 * 1024 * 1024
MAX_NAME_CHARS = 100
# A text share is short; anything bigger saved as a file is a file, not a note.
TEXT_MESSAGE_MAX = 64 * 1024
TEXT_FILE_TYPE = "text/plain"

APPROVAL_ACTION = "localsend_receive"


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def enabled_in(env: dict, cfg=None) -> bool:
    """Is the receiver switched on? An explicit env value wins, else the saved preference."""
    raw = env.get("LANTERN_LOCALSEND")
    if raw is not None and str(raw).strip() != "":
        return _flag(str(raw))
    return bool(getattr(cfg, "localsend", False))


def pin_in(env: dict) -> str | None:
    pin = (env.get("LANTERN_LOCALSEND_PIN") or "").strip()
    return pin or None


def port_in(env: dict) -> int:
    try:
        return int(env.get("LANTERN_LOCALSEND_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def inbox_in(env: dict) -> Path:
    custom = (env.get("LANTERN_LOCALSEND_INBOX") or "").strip()
    if custom:
        return Path(custom).expanduser()
    return default_inbox()


def default_inbox() -> Path:
    return Path.home() / "Downloads" / "Lantern Inbox"


def lantern_home() -> Path:
    return Path(os.environ.get("LANTERN_HOME", "~/.lantern")).expanduser()


def fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


# ---- file names ---------------------------------------------------------------
def sanitize_name(name: str) -> str:
    """A sender-supplied name made safe: no directories, no dot-files, length-limited."""
    base = str(name or "").replace("\\", "/").split("/")[-1].strip()
    base = "".join(c for c in base if ord(c) >= 32)
    base = base.strip().strip(".")
    if base in ("", ".", ".."):
        base = "file"
    if len(base) > MAX_NAME_CHARS:
        stem, dot, ext = base.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            base = stem[: MAX_NAME_CHARS - len(ext) - 1] + "." + ext
        else:
            base = base[:MAX_NAME_CHARS]
    return base


def unique_name(folder: Path, name: str) -> str:
    """A name that does not exist yet in folder: ``photo (2).jpg``. Never overwrites."""
    if not (folder / name).exists():
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 2
    while True:
        candidate = f"{stem} ({n})" + (f".{ext}" if ext else "")
        if not (folder / candidate).exists():
            return candidate
        n += 1


# ---- identity -----------------------------------------------------------------
@dataclass
class Identity:
    key_file: Path
    cert_file: Path
    cert_der: bytes
    fingerprint: str


def fingerprint_of(cert_der: bytes) -> str:
    """The spec's fingerprint for HTTPS: SHA-256 of the certificate."""
    return hashlib.sha256(cert_der).hexdigest().upper()


def ensure_identity(folder: str | Path, alias: str) -> Identity:
    """Load the self-signed certificate in folder, or make and keep one."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as dt

    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    key_file, cert_file = folder / "key.pem", folder / "cert.pem"
    if key_file.exists() and cert_file.exists():
        from cryptography.x509 import load_pem_x509_certificate
        cert = load_pem_x509_certificate(cert_file.read_bytes())
        cert_der = cert.public_bytes(serialization.Encoding.DER)
        return Identity(key_file, cert_file, cert_der, fingerprint_of(cert_der))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, alias[:64])])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName("localhost"),
             x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    return Identity(key_file, cert_file, cert_der, fingerprint_of(cert_der))


# ---- transfers ------------------------------------------------------------------
@dataclass
class _Offer:
    id: str
    name: str
    size: int
    file_type: str
    sha256: str | None
    token: str
    message: bool = False
    received: bool = False
    saved_as: str | None = None


@dataclass
class _Session:
    id: str
    sender: str
    files: dict[str, _Offer] = field(default_factory=dict)


class Receiver:
    """The protocol logic, without sockets: methods take plain values and
    return (HTTP status, JSON body), so tests run in-process."""

    def __init__(self, policy, gate: Gate | Callable[[str], bool], *,
                 audit=None, on_link: Callable[[str], Any] | None = None,
                 emit: Callable[[dict], None] | None = None,
                 home: str | Path | None = None, inbox: str | Path | None = None,
                 pin: str | None = None):
        self.policy = policy
        self.gate = as_gate(gate)
        self.audit = audit
        self.on_link = on_link
        self.emit = emit
        self.home = Path(home).expanduser() if home else lantern_home()
        self.inbox = Path(inbox).expanduser() if inbox else default_inbox()
        self.pin = pin or None
        self.alias = f"Lantern on {socket.gethostname()}"
        self.identity = ensure_identity(self.home / "localsend", self.alias)
        self.port = DEFAULT_PORT
        self.texts: list[dict] = []       # text shares to show in the panel
        self.peers: dict[str, str] = {}   # fingerprint -> alias (discovery only)
        self.discovery_error: str | None = None
        self._session: _Session | None = None
        self._lock = threading.Lock()

    @property
    def fingerprint(self) -> str:
        return self.identity.fingerprint

    # ---- our device description -------------------------------------------------
    def info(self) -> dict:
        import platform
        return {"alias": self.alias, "version": PROTOCOL_VERSION,
                "deviceModel": platform.system(), "deviceType": "desktop",
                "fingerprint": self.fingerprint, "port": self.port,
                "protocol": "https", "download": False}

    def announce(self, yes: bool = True) -> dict:
        return {**self.info(), "announce": yes}

    def register(self, peer: Any) -> dict:
        """A phone saying hello (multicast or HTTP legacy mode)."""
        if isinstance(peer, dict):
            fp, alias = peer.get("fingerprint"), peer.get("alias")
            if isinstance(fp, str) and fp and isinstance(alias, str) and alias:
                self.peers[fp] = alias[:120]
        return {**self.info()}

    # ---- prepare / upload / cancel ------------------------------------------------
    def check_ready(self) -> Path:
        """Refuse to start unless the inbox is a writable zone."""
        try:
            self.policy.check_writable(self.inbox)
        except PolicyError as e:
            raise RuntimeError(
                f"LocalSend inbox {self.inbox} is not in a writable zone ({e}). "
                "Pick a writable folder or set LANTERN_LOCALSEND_INBOX.") from e
        self.inbox.mkdir(parents=True, exist_ok=True)
        return self.inbox

    def prepare(self, body: Any, pin: str | None = None) -> tuple[int, dict]:
        if self.pin and not hmac.compare_digest(str(pin or ""), self.pin):
            return 401, {"error": "PIN required"}
        if not isinstance(body, dict) or not isinstance(body.get("files"), dict) or not body["files"]:
            return 400, {"error": "invalid body: need info and files"}
        files = body["files"]
        if len(files) > MAX_FILES:
            return 403, {"error": f"too many files (at most {MAX_FILES})"}
        info = body.get("info") if isinstance(body.get("info"), dict) else {}
        sender = str(info.get("alias") or "a phone")[:120]
        offers: list[_Offer] = []
        total = 0
        for fid, f in files.items():
            if not isinstance(f, dict):
                return 400, {"error": f"invalid file entry {fid!r}"}
            try:
                size = int(f.get("size", -1))
            except (TypeError, ValueError):
                return 400, {"error": f"invalid size for {fid!r}"}
            if size < 0:
                return 400, {"error": f"invalid size for {fid!r}"}
            name = str(f.get("fileName") or "file")
            sha = f.get("sha256")
            ftype = str(f.get("fileType") or "")
            total += size
            offers.append(_Offer(id=str(fid), name=name, size=size, file_type=ftype,
                                 sha256=str(sha) if sha else None,
                                 token=secrets.token_urlsafe(32),
                                 message=(ftype == TEXT_FILE_TYPE and size <= TEXT_MESSAGE_MAX)))
        if total > MAX_TRANSFER_BYTES:
            return 403, {"error": f"transfer too big (at most {fmt_size(MAX_TRANSFER_BYTES)})"}
        with self._lock:
            if self._session is not None:
                return 409, {"error": "blocked by another session"}

        session_id = secrets.token_urlsafe(16)
        files_bit = "; ".join(f"{o.name} ({fmt_size(o.size)})"
                              for o in offers if not o.message) or "no files"
        texts = [o for o in offers if o.message]
        if texts:
            preview = ""
            if isinstance(body.get("files", {}).get(texts[0].id, {}).get("preview"), str):
                preview = str(body["files"][texts[0].id]["preview"])[:120]
            summary = (f"\U0001f4f1 {sender} wants to share text"
                       + (f": {preview}" if preview else f" ({len(texts)} message{'s' if len(texts) != 1 else ''})"))
        else:
            summary = (f"\U0001f4f1 {sender} wants to send {len(offers)} "
                       f"file{'s' if len(offers) != 1 else ''}: {files_bit}")
        payload = {"session": session_id, "sender": sender,
                   "files": [{"id": o.id, "name": o.name, "size": o.size,
                              "sha256": o.sha256, "message": o.message} for o in offers]}
        commit = self.gate.ask(APPROVAL_ACTION, summary, payload, tainted=True)
        if commit is None:
            self._audit("prepare", sender, offers, "declined")
            return 403, {"error": "rejected"}
        try:
            commit(payload)
        except ApprovalError as e:
            self._audit("prepare", sender, offers, "declined")
            return 403, {"error": str(e)}
        with self._lock:
            if self._session is not None:
                return 409, {"error": "blocked by another session"}
            self._session = _Session(id=session_id, sender=sender,
                                     files={o.id: o for o in offers})
        self._audit("prepare", sender, offers, "accepted")
        return 200, {"sessionId": session_id, "files": {o.id: o.token for o in offers}}

    def upload(self, session_id: str | None, file_id: str | None,
               token: str | None, data: bytes) -> tuple[int, dict]:
        with self._lock:
            session = self._session
            offer = session.files.get(str(file_id)) if session else None
        if session is None or not hmac.compare_digest(session.id, str(session_id or "")):
            return 403, {"error": "unknown session"}
        if offer is None:
            return 403, {"error": "unknown file"}
        if not hmac.compare_digest(offer.token, str(token or "")):
            return 403, {"error": "invalid token"}
        if len(data) != offer.size:
            return 403, {"error": f"size mismatch (announced {offer.size}, got {len(data)})"}
        if offer.sha256 and hashlib.sha256(data).hexdigest() != offer.sha256.lower():
            return 422, {"error": "checksum mismatch"}
        if offer.message:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return 400, {"error": "message is not text"}
            self.receive_text(session.sender, text)
            self._mark_received(session, offer)
            return 200, {}
        name = unique_name(self.inbox, sanitize_name(offer.name))
        try:
            self.policy.write_bytes(self.inbox / name, data)
        except PolicyError as e:
            return 500, {"error": str(e)}
        saved = self.inbox / name
        if not saved.is_file() or saved.stat().st_size != offer.size:  # verify on disk
            return 500, {"error": "not saved correctly"}
        offer.saved_as = name
        self._audit("upload", session.sender, [offer], "accepted")
        self._mark_received(session, offer)
        return 200, {"saved": name, "verified": True}

    def cancel(self, session_id: str | None) -> tuple[int, dict]:
        with self._lock:
            if self._session is not None and hmac.compare_digest(self._session.id, str(session_id or "")):
                self._session = None
        return 200, {}

    def _mark_received(self, session: _Session, offer: _Offer) -> None:
        with self._lock:
            offer.received = True
            if all(o.received for o in session.files.values()) and self._session is session:
                self._session = None

    # ---- text and links -------------------------------------------------------------
    def receive_text(self, sender: str, text: str) -> dict:
        """A text share: a web link submits the T1 goal, other text waits in the panel."""
        url = linkopen.clean_url(text or "")
        if url is not None:
            goal = f"[localsend] open this link on my laptop: {url}"
            task_id = self.on_link(url) if self.on_link else None
            if self.emit:
                self.emit({"type": "localsend_link", "sender": sender, "url": url, "task_id": task_id})
            return {"kind": "link", "url": url, "goal": goal, "task_id": task_id}
        entry = {"sender": sender, "text": text}
        self.texts.append(entry)
        if self.emit:
            self.emit({"type": "localsend_text", **entry})
        return {"kind": "text", "sender": sender}

    # ---- audit -------------------------------------------------------------------------
    def _audit(self, kind: str, sender: str, offers: list[_Offer], decision: str) -> None:
        if self.audit is None:
            return
        meta = {"sender": sender, "decision": decision,
                "files": [{"name": o.name, "size": o.size,
                           "saved_as": o.saved_as, "message": o.message} for o in offers]}
        names = ", ".join(o.saved_as or o.name for o in offers) or "no files"
        total = sum(o.size for o in offers)
        self.audit.record(lane="local", destination="localsend", endpoint=f"localsend.{kind}",
                          model="localsend", payload=json.dumps(meta).encode(),
                          preview=f"{sender}: {names} ({fmt_size(total)}) — {decision}")

    # ---- status file (the doctor reads this; it never holds contents) -------------------
    def write_status(self, listening: bool, port: int) -> None:
        try:
            folder = self.home / "localsend"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "status.json").write_text(json.dumps(
                {"listening": listening, "port": port, "alias": self.alias,
                 "fingerprint": self.fingerprint, "inbox": str(self.inbox),
                 "discovery_error": self.discovery_error}), encoding="utf-8")
        except OSError:
            pass


# ---- serving: HTTPS + multicast discovery ------------------------------------------
def _make_handler(receiver: Receiver):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "LanternLocalSend/1.0"

        def log_message(self, *args):  # phones poll; keep the terminal quiet
            pass

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length > 1024 * 1024:
                return None
            try:
                return json.loads(self.rfile.read(length) or b"null")
            except (ValueError, OSError):
                return None

        def do_GET(self) -> None:
            if urlsplit(self.path).path == "/api/localsend/v2/info":
                self._json(200, receiver.info())
            else:
                self._json(404, {"error": "no such route"})

        def do_POST(self) -> None:
            parts = urlsplit(self.path)
            path, query = parts.path, parts.query
            args = {k: v[0] for k, v in parse_qs(query).items()}
            if path == "/api/localsend/v2/register":
                self._json(200, receiver.register(self._read_json()))
            elif path == "/api/localsend/v2/prepare-upload":
                code, obj = receiver.prepare(self._read_json(), pin=args.get("pin"))
                self._json(code, obj)
            elif path == "/api/localsend/v2/upload":
                try:
                    length = int(self.headers.get("Content-Length") or -1)
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_TRANSFER_BYTES + 1024 * 1024:
                    self._json(403, {"error": "bad size"})
                    return
                try:
                    data = self.rfile.read(length)
                except OSError:
                    self._json(500, {"error": "could not read upload"})
                    return
                code, obj = receiver.upload(args.get("sessionId"), args.get("fileId"),
                                            args.get("token"), data)
                self._json(code, obj)
            elif path == "/api/localsend/v2/cancel":
                code, obj = receiver.cancel(args.get("sessionId"))
                self._json(code, obj)
            else:
                self._json(404, {"error": "no such route"})

    return Handler


class _ThreadServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Server:
    """Binds the receiver: an HTTPS server on the LAN plus multicast discovery."""

    def __init__(self, receiver: Receiver, port: int = DEFAULT_PORT):
        self.receiver = receiver
        self.port = port
        self.listening = False
        self._httpd: _ThreadServer | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def status(self) -> dict:
        return {"listening": self.listening, "port": self.port, "alias": self.receiver.alias,
                "inbox": str(self.receiver.inbox),
                "discovery_error": self.receiver.discovery_error}

    def start(self) -> dict:
        if self.listening:
            return self.status()
        self.receiver.port = self.port
        self.receiver.check_ready()  # raises RuntimeError outside a writable zone
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.receiver.identity.cert_file),
                                str(self.receiver.identity.key_file))
        self._httpd = _ThreadServer(("0.0.0.0", self.port), _make_handler(self.receiver))
        self.port = self._httpd.socket.getsockname()[1]
        self.receiver.port = self.port
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self._stop.clear()
        for target, name in ((self._httpd.serve_forever, "localsend-https"),
                             (self._discover, "localsend-discovery")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        self.listening = True
        self.receiver.write_status(True, self.port)
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except OSError:
                pass
            try:
                self._httpd.server_close()
            except OSError:
                pass
            self._httpd = None
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads = []
        self.listening = False
        self.receiver.write_status(False, self.port)

    # ---- discovery ------------------------------------------------------------------
    def _discover(self) -> None:
        """Announce ourselves; answer others. Any socket problem stays a message,
        never a crash: the HTTPS server keeps working without discovery."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        except OSError as e:
            return self._note(f"no network for discovery ({e})")
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", self.port))
            except OSError as e:
                return self._note(f"can't listen for discovery on UDP port {self.port} ({e})")
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                mreq = struct.pack("4s4s", socket.inet_aton(MULTICAST_ADDR),
                                   socket.inet_aton("0.0.0.0"))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as e:
                return self._note(f"can't join the discovery group {MULTICAST_ADDR} ({e})")
            try:
                sock.sendto(json.dumps(self.receiver.announce(True)).encode(),
                            (MULTICAST_ADDR, self.port))
            except OSError as e:
                return self._note(f"can't announce on {MULTICAST_ADDR} ({e})")
            sock.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._answer(data, addr[0])
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _note(self, message: str) -> None:
        self.receiver.discovery_error = message
        self.receiver.write_status(self.listening, self.port)

    def _answer(self, data: bytes, sender_ip: str) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if not isinstance(msg, dict) or not msg.get("announce"):
            return
        if msg.get("fingerprint") == self.receiver.fingerprint:
            return  # ourselves
        try:
            peer_port = int(msg.get("port") or DEFAULT_PORT)
        except (TypeError, ValueError):
            peer_port = DEFAULT_PORT
        self._register_with(sender_ip, peer_port)
        try:  # fallback: a multicast answer, in case the HTTP one can't get through
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                sock.sendto(json.dumps(self.receiver.announce(False)).encode(),
                            (MULTICAST_ADDR, self.port))
            finally:
                sock.close()
        except OSError:
            pass

    def _register_with(self, host: str, port: int) -> None:
        """Answer an announcement over HTTP, as the spec asks, then fall back."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            conn = http.client.HTTPSConnection(host, port, timeout=5, context=context)
            try:
                body = json.dumps(self.receiver.register(self.receiver.info())).encode()
                conn.request("POST", "/api/localsend/v2/register", body,
                             {"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
                conn.getresponse().read()
            finally:
                conn.close()
        except (OSError, http.client.HTTPException, ssl.SSLError):
            pass
