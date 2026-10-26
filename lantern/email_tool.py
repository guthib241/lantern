"""Email: draft -> approval card -> send over SMTP -> find it in Sent over IMAP.

The approval is bound to exactly what will go out: recipients, subject, SHA-256 of the
body, and name + SHA-256 + size of every attachment. Right before sending, the payload is
rebuilt from disk; if anything changed (an attachment edited after you said yes), it is
refused. Attachments are read through policy.py: hidden files never, private files only
with the card marking them PRIVATE. The card also warns when
  - a recipient appeared only in untrusted content (a file, web page or email) and not in
    your request or Lantern's memory: the classic prompt-injection exfiltration move,
  - the body looks like it carries a secret or an ID number.
After sending, `verified` is true only if IMAP finds the Message-ID in the Sent folder
(Gmail files SMTP mail there automatically).

Modes (LANTERN_EMAIL_MODE): "fake" (default; writes .eml files to ~/.lantern/media/outbox and
keeps an in-memory Sent folder) or "smtp" (Gmail: smtp.gmail.com:465 + an app password).
"""
from __future__ import annotations

import hashlib
import imaplib
import mimetypes
import re
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Callable

from .policy import HIDDEN, PRIVATE, Policy, PolicyError
from .scrubber import DEFAULT as SCRUBBER

MAX_ATTACH_BYTES = 20 * 1024 * 1024
ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
EMAILS_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


class EmailError(Exception):
    """The email can't be sent; the message is safe to show the model."""


@dataclass
class EmailSettings:
    mode: str = "fake"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    user: str = ""
    password: str = ""
    from_addr: str = ""
    imap_host: str = "imap.gmail.com"
    sent_folder: str = "[Gmail]/Sent Mail"
    verify_wait: float = 15.0

    @classmethod
    def from_env(cls, env: dict) -> "EmailSettings":
        user = env.get("LANTERN_SMTP_USER", "")
        return cls(mode=env.get("LANTERN_EMAIL_MODE", "fake"),
                   smtp_host=env.get("LANTERN_SMTP_HOST", cls.smtp_host),
                   smtp_port=int(env.get("LANTERN_SMTP_PORT", cls.smtp_port)),
                   user=user, password=env.get("LANTERN_SMTP_PASSWORD", ""),
                   from_addr=env.get("LANTERN_EMAIL_FROM", user),
                   imap_host=env.get("LANTERN_IMAP_HOST", cls.imap_host),
                   sent_folder=env.get("LANTERN_SENT_FOLDER", cls.sent_folder))


# ---- mailers ------------------------------------------------------------------------------
class FakeMailer:
    """Offline: keeps a Sent folder in memory and drops .eml copies in an outbox folder."""

    destination = "fake://smtp"

    def __init__(self, outbox_dir: str | Path | None = None, file_in_sent: bool = True):
        self.outbox_dir = Path(outbox_dir).expanduser() if outbox_dir else None
        self.file_in_sent = file_in_sent  # False simulates a provider that doesn't keep sent mail
        self.sent: list[EmailMessage] = []

    def send(self, msg: EmailMessage) -> None:
        if self.file_in_sent:
            self.sent.append(msg)
        if self.outbox_dir is not None:
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9.@-]", "_", msg["Message-ID"].strip("<>"))
            (self.outbox_dir / f"{name}.eml").write_bytes(msg.as_bytes())

    def find_sent(self, message_id: str) -> bool:
        return any(m["Message-ID"] == message_id for m in self.sent)


class SmtpMailer:
    """SMTP over SSL to send, IMAP over SSL to find the message in Sent."""

    def __init__(self, s: EmailSettings, smtp_factory: Callable[..., Any] = smtplib.SMTP_SSL,
                 imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL):
        if not (s.user and s.password):
            raise EmailError("email isn't set up: set LANTERN_SMTP_USER and LANTERN_SMTP_PASSWORD "
                             "(a Gmail app password), or LANTERN_EMAIL_MODE=fake")
        self.s = s
        self.smtp_factory = smtp_factory
        self.imap_factory = imap_factory
        self.destination = f"smtp://{s.smtp_host}:{s.smtp_port}"

    def send(self, msg: EmailMessage) -> None:
        with self.smtp_factory(self.s.smtp_host, self.s.smtp_port, timeout=30) as smtp:
            smtp.login(self.s.user, self.s.password)
            smtp.send_message(msg)

    def find_sent(self, message_id: str) -> bool:
        imap = self.imap_factory(self.s.imap_host, 993)
        try:
            imap.login(self.s.user, self.s.password)
            typ, _ = imap.select(f'"{self.s.sent_folder}"', readonly=True)
            if typ != "OK":
                return False
            typ, data = imap.search(None, "HEADER", "Message-ID", message_id)
            return typ == "OK" and bool(data and data[0] and data[0].split())
        finally:
            try:
                imap.logout()
            except Exception:
                pass


def make_mailer(s: EmailSettings, outbox_dir: str | Path | None = None):
    if s.mode == "fake":
        return FakeMailer(outbox_dir)
    if s.mode == "smtp":
        return SmtpMailer(s)
    raise EmailError(f"unknown LANTERN_EMAIL_MODE {s.mode!r}; use fake or smtp")


# ---- the service ----------------------------------------------------------------------------
def _addresses(value: Any) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[,;]", str(value or ""))
    out = []
    for item in items:
        addr = parseaddr(str(item).strip())[1].strip()
        if not addr:
            continue
        if not ADDRESS.match(addr):
            raise EmailError(f"'{item}' is not an email address")
        out.append(addr.lower())
    return sorted(set(out))


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB"


class EmailService:
    def __init__(self, settings: EmailSettings, policy: Policy, audit=None, mailer=None,
                 outbox_dir: str | Path | None = None):
        self.settings = settings
        self.policy = policy
        self.audit = audit
        self._mailer = mailer
        self.outbox_dir = outbox_dir

    @property
    def mailer(self):
        if self._mailer is None:
            self._mailer = make_mailer(self.settings, self.outbox_dir)
        return self._mailer

    @property
    def from_addr(self) -> str:
        return self.settings.from_addr or self.settings.user or "lantern@localhost"

    def _attachments(self, paths: list[str]) -> list[dict]:
        out, total = [], 0
        for raw in paths or []:
            try:
                p, zone = self.policy.check_visible(raw)
            except PolicyError as e:
                raise EmailError(str(e)) from e
            if zone == HIDDEN or not p.is_file():
                raise EmailError(f"{raw} is not a file I can attach")
            data = self.policy.read_bytes(p, allow_private=True)  # private: shown as PRIVATE on the card
            total += len(data)
            if total > MAX_ATTACH_BYTES:
                raise EmailError("attachments are over 20 MB together")
            out.append({"name": p.name, "path": str(p), "data": data, "private": zone == PRIVATE,
                        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        return out

    def plan(self, to: list[str], cc: list[str], subject: str, body: str, files: list[dict]) -> dict:
        """The approval payload: what will go out, as hashes."""
        return {"from": self.from_addr, "to": to, "cc": cc, "subject": subject,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "body_chars": len(body),
                "attachments": [{k: f[k] for k in ("name", "sha256", "bytes", "private")} for f in files]}

    def send(self, to: Any, subject: str, body: str, attachments: list[str] | None = None, cc: Any = None, *,
             approve: Callable, commit: Callable, untrusted_emails: set[str] | None = None,
             known_emails: set[str] | None = None, task_id: int | None = None) -> dict:
        to_list, cc_list = _addresses(to), _addresses(cc)
        if not to_list:
            raise EmailError("no recipient")
        subject = (subject or "").strip()
        if "\n" in subject or "\r" in subject:
            raise EmailError("the subject must be one line")
        files = self._attachments(attachments or [])
        payload = self.plan(to_list, cc_list, subject, body, files)

        warnings = []
        suspicious = sorted(a for a in to_list + cc_list
                            if a in (untrusted_emails or set()) and a not in (known_emails or set()))
        if suspicious:
            warnings.append(f"⚠️ {', '.join(suspicious)} appeared only in a file/web page, not in your request")
        leaked = SCRUBBER.scrub(body).redactions
        if leaked:
            warnings.append("⚠️ the body looks like it contains: " + ", ".join(sorted(leaked)))
        if any(f["private"] for f in files):
            warnings.append("🔒 includes PRIVATE files: they will leave this computer")
        att = "; ".join(f"{f['name']} ({_size(f['bytes'])}{', PRIVATE' if f['private'] else ''})" for f in files)
        preview = re.sub(r"\s+", " ", body).strip()
        summary = (f"Send email to {', '.join(to_list)}" + (f" (cc {', '.join(cc_list)})" if cc_list else "")
                   + f": “{subject}”. Body ({len(body)} chars): {preview[:200]}{'…' if len(preview) > 200 else ''}"
                   + (f" Attachments: {att}." if files else "") + "".join(" " + w for w in warnings))

        ok = approve("send_email", summary, payload)
        if ok is None:
            return {"sent": False, "verified": False, "reason": "user declined"}
        final_files = self._attachments(attachments or [])   # re-read: what's on disk now is what goes out
        if refused := commit(ok, self.plan(to_list, cc_list, subject, body, final_files)):
            return {"sent": False, "verified": False, "reason": refused["reason"]}

        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.from_addr.split("@")[-1] or "lantern.local")
        msg.set_content(body)
        for f in final_files:
            ctype = mimetypes.guess_type(f["name"])[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(f["data"], maintype=maintype, subtype=subtype, filename=f["name"])
        raw = msg.as_bytes()
        try:
            self.mailer.send(msg)
        except (smtplib.SMTPException, OSError) as e:
            raise EmailError(f"sending failed: {type(e).__name__}: {str(e)[:160]}") from e
        if self.audit is not None:
            self.audit.record(lane="cloud", destination=self.mailer.destination, endpoint="smtp.send",
                              model="email", payload=raw, task_id=task_id,
                              preview=f"to {msg['To']}: {subject}")
        verified = self._verify(msg["Message-ID"])
        result = {"sent": True, "verified": verified, "message_id": msg["Message-ID"], "to": to_list,
                  "cc": cc_list, "subject": subject, "attachments": [f["name"] for f in final_files]}
        if not verified:
            result["note"] = "sent, but I couldn't find it in the Sent folder to confirm"
        return result

    def _verify(self, message_id: str) -> bool:
        end = time.time() + self.settings.verify_wait
        delay = 0.5
        while True:
            try:
                if self.mailer.find_sent(message_id):
                    return True
            except (imaplib.IMAP4.error, OSError):
                pass
            if time.time() >= end:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 4)


def emails_in(text: str) -> set[str]:
    return {e.lower() for e in EMAILS_IN_TEXT.findall(text or "")}
