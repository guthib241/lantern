"""Remove secrets and personal ID numbers from anything that leaves the laptop.

Runs on every cloud payload (see audit.GuardedClient), in code, not in a prompt.
Each match becomes `[REDACTED:<kind>]`. Patterns that often look like ordinary
numbers are checked before redacting: card numbers must pass Luhn, Aadhaar
numbers must pass Verhoeff.

Covered:
  secrets   private key blocks, AWS / GitHub / GitLab / Slack / Stripe / Google /
            OpenAI-style / Hugging Face / Meta Model API keys, JWTs, bearer tokens,
            `password=...` / `api_key: ...` style assignments, URLs with passwords
  India     Aadhaar, PAN, UPI IDs
  other     payment card numbers, US SSNs, IBANs
Emails and phone numbers are kept by default (Lantern needs them to send email
or reply on WhatsApp); turn them on with Scrubber(emails=True, phones=True).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

Check = Callable[[str], bool]


def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_V_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_V_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_ok(number: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed([d for d in number if d.isdigit()])):
        c = _V_D[c][_V_P[i % 8][int(ch)]]
    return c == 0


@dataclass(frozen=True)
class Pattern:
    kind: str
    regex: re.Pattern
    group: int = 0            # which group to redact (0 = whole match)
    check: Check | None = None


def _p(kind: str, rx: str, group: int = 0, check: Check | None = None, flags: int = 0) -> Pattern:
    return Pattern(kind, re.compile(rx, flags), group, check)


SECRET_PATTERNS = [
    _p("private_key", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"),
    _p("aws_access_key", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"),
    _p("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    _p("gitlab_token", r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    _p("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    _p("stripe_key", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    _p("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    _p("huggingface_token", r"\bhf_[A-Za-z0-9]{30,}\b"),
    _p("meta_model_key", r"\bLLM\|[A-Za-z0-9|_\-]{16,}"),
    _p("api_key", r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}\b"),
    _p("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    _p("bearer_token", r"\b[Bb]earer\s+([A-Za-z0-9_\-\.=+/]{20,})", group=1),
    _p("url_password", r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:([^\s@/]+)@", group=1),
    _p("password_assignment",
       r"(?i)\b(?:password|passwd|pwd|passphrase|secret|api[_\-]?key|access[_\-]?token|auth[_\-]?token|"
       r"client[_\-]?secret|private[_\-]?key)\b[\"']?\s*[:=]\s*[\"']?([^\s\"',;]{6,})", group=1),
    # S0: env-style names (LANTERN_DEMO_API_KEY=..., DB_PASSWORD=...): the \b above can't see a word after "_"
    _p("env_secret", r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASS|PWD)\b\s*[:=]\s*"
                     r"[\"']?([^\s\"',;]{6,})", group=1),
    # S0: one-time codes and card security codes after their label ("last OTP: 482913", "CVV 123")
    _p("one_time_code", r"(?i)\b(?:OTP|one[- ]time (?:password|passcode|code)|verification code|security code|CVV|"
                        r"CVC)\b\s*(?:is|:|=|-|#)?\s*(\d{3,8})(?!\d)", group=1),
]

PII_PATTERNS = [
    _p("aadhaar", r"(?<![\d-])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\d-])", check=verhoeff_ok),
    _p("pan", r"\b[A-Z]{3}[ABCFGHLJPTK][A-Z]\d{4}[A-Z]\b"),
    _p("upi_id", r"\b[A-Za-z0-9.\-_]{2,256}@(?:ok(?:axis|hdfcbank|icici|sbi)|ybl|ibl|axl|paytm|upi|apl|"
                 r"yapl|ptyes|pthdfc|ptsbi|ptaxis|axisbank|hdfcbank|icici|sbi|kotak|idfcbank|"
                 r"barodampay|freecharge|jupiteraxis|fbl|airtel|waicici|wahdfcbank)\b(?!\.)"),
    _p("card_number", r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", check=luhn_ok),
    _p("us_ssn", r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    _p("iban", r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"),
]

EMAIL = _p("email", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE = _p("phone", r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{4}[ -]?\d{5}(?!\d)|\+\d{1,3}[ -]?\d{3,4}[ -]?\d{3,4}[ -]?\d{3,4}")


@dataclass
class ScrubResult:
    text: str
    redactions: Counter = field(default_factory=Counter)

    @property
    def count(self) -> int:
        return sum(self.redactions.values())


class Scrubber:
    def __init__(self, emails: bool = False, phones: bool = False, extra: list[Pattern] | None = None):
        self.patterns = list(SECRET_PATTERNS) + list(PII_PATTERNS) + list(extra or [])
        if emails:
            self.patterns.append(EMAIL)
        if phones:
            self.patterns.append(PHONE)

    def scrub(self, text: str) -> ScrubResult:
        found: Counter = Counter()
        for pat in self.patterns:
            text = pat.regex.sub(lambda m, pat=pat: self._replace(m, pat, found), text)
        return ScrubResult(text, found)

    @staticmethod
    def _replace(m: re.Match, pat: Pattern, found: Counter) -> str:
        target = m.group(pat.group)
        if not target or target.startswith("[REDACTED:"):
            return m.group(0)
        if pat.check and not pat.check(target):
            return m.group(0)
        found[pat.kind] += 1
        if pat.group == 0:
            return f"[REDACTED:{pat.kind}]"
        start, end = m.start(pat.group) - m.start(0), m.end(pat.group) - m.start(0)
        whole = m.group(0)
        return whole[:start] + f"[REDACTED:{pat.kind}]" + whole[end:]

    def scrub_obj(self, obj: Any, found: Counter | None = None) -> tuple[Any, Counter]:
        """Scrub every string inside dicts/lists (keys are left alone).
        Base64 data URLs (screenshots) are left untouched: regexes can't see pixels, and a
        PAN- or IBAN-shaped run inside the base64 would otherwise corrupt the image."""
        found = Counter() if found is None else found
        if isinstance(obj, str) and is_data_url(obj):
            return obj, found
        if isinstance(obj, str):
            decoded = _json_container(obj)
            if decoded is not None:
                # Tool results travel as JSON strings: a secret at the start of a line follows a
                # literal "\n", which defeats \b. Scrub the decoded values; re-encode only if needed,
                # so clean payloads stay byte-identical (prompt caching).
                inner: Counter = Counter()
                cleaned, _ = self.scrub_obj(decoded, inner)
                if inner:
                    found.update(inner)
                    obj = json.dumps(cleaned, ensure_ascii=False)  # then scrubbed as text too (keys)
            r = self.scrub(obj)
            found.update(r.redactions)
            return r.text, found
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v, found)[0] for k, v in obj.items()}, found
        if isinstance(obj, (list, tuple)):
            return [self.scrub_obj(v, found)[0] for v in obj], found
        return obj, found


# Wire fields of a Responses / Chat input item. They carry ids, never user content, and a
# "redacted" call_id (a Luhn-valid digit run) would break the function-call chain.
WIRE_KEYS = frozenset({"id", "call_id", "type", "role", "status"})


def scrub_payload(scrubber: "Scrubber", value: Any, found: Counter | None = None) -> tuple[Any, Counter]:
    """Scrub an `input` / `messages` / `instructions` value: like scrub_obj, except that each
    top-level item's wire ids (WIRE_KEYS) pass untouched. Everything nested is scrubbed."""
    found = Counter() if found is None else found
    if not isinstance(value, list):
        return scrubber.scrub_obj(value, found)
    out = []
    for item in value:
        if isinstance(item, dict):
            out.append({k: v if k in WIRE_KEYS and isinstance(v, str) else scrubber.scrub_obj(v, found)[0]
                        for k, v in item.items()})
        else:
            out.append(scrubber.scrub_obj(item, found)[0])
    return out, found


def _json_container(text: str) -> Any:
    """The decoded value if `text` is a JSON object or array, else None."""
    if text.lstrip()[:1] not in ("{", "["):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, (dict, list)) else None


DATA_URL = re.compile(r"^data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]*$")


def is_data_url(text: str) -> bool:
    return text.startswith("data:") and DATA_URL.match(text) is not None


DEFAULT = Scrubber()


def scrub(text: str) -> ScrubResult:
    return DEFAULT.scrub(text)
