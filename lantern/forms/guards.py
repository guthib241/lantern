"""Hard stops for form filling, enforced in code, never left to a prompt.

Lantern never types into:
  - password fields (`type=password`, autocomplete current-/new-password),
  - one-time codes (`autocomplete=one-time-code`, "OTP", "verification code"),
  - payment card fields (`autocomplete=cc-*`, "card number", "CVV", "PIN"),
  - government ID fields (Aadhaar, PAN, passport, SSN, licence, tax ID),
  - anything inside a payment frame (Stripe, Razorpay, PayPal, Adyen, Braintree...),
and it stops at CAPTCHAs: "you do this part".

`classify(field)` looks at a field description from the browser driver and returns
a short reason when the field is off limits, or None. `value_reason(text)` refuses
values that look like card numbers, ID numbers or secrets even in a harmless field.
"""
from __future__ import annotations

import re

from ..scrubber import DEFAULT as SCRUBBER

PAYMENT_HINTS = ("stripe", "razorpay", "paypal", "adyen", "braintree", "checkout.com", "cashfree",
                 "payu", "paytm", "squareup", "square.com", "worldpay", "klarna", "card payment")
CAPTCHA_HINTS = ("captcha", "recaptcha", "hcaptcha", "turnstile", "challenges.cloudflare", "arkoselabs",
                 "funcaptcha", "geetest", "i'm not a robot", "im not a robot")

# Matched against normalized text: camelCase split, separators turned into spaces, lower case.
_SENSITIVE = re.compile(
    r"\b(?P<password>password|passwd|pass ?code|passphrase|pwd)\b"
    r"|\b(?P<otp>otp|one time|verification code|security code|auth code|2fa code)\b"
    r"|\b(?P<card>cards?|cardholder|cvv\d?|cvc\d?|csc|pin(?! ?code)|mpin)\b"
    r"|\b(?P<gov_id>aadhaa?r|pan|permanent account number|passport|ssn|social security|national id|"
    r"voter id|driving licen[cs]e|driver'?s? licen[cs]e|tax id)\b"
    r"|\b(?P<captcha>captcha|recaptcha|hcaptcha)\b"
)
REASONS = {
    "password": "password field",
    "otp": "one-time code",
    "card": "payment card field",
    "gov_id": "government ID number",
    "captcha": "CAPTCHA",
    "payment_frame": "payment frame",
}
SCRUB_REASONS = {
    "card_number": "a payment card number", "aadhaar": "an Aadhaar number", "pan": "a PAN",
    "us_ssn": "an SSN", "iban": "a bank IBAN", "private_key": "a private key",
}
NAV_KEYS = {"Tab", "Shift+Tab", "Escape"}


class HardStop(Exception):
    """An action Lantern refuses to take on the user's behalf."""

    def __init__(self, reason: str, *, captcha: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.captcha = captcha


def normalize(text: str) -> str:
    """'userPanNumber' / 'card_number' / 'cc-exp' -> 'user pan number' / 'card number' / 'cc exp'."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text or "")
    text = re.sub(r"[_\-.:\[\]/()*#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def frame_reason(desc: str) -> str | None:
    """Reason a frame (described by its src/title/name/id) is off limits."""
    d = (desc or "").lower()
    if any(h in d for h in CAPTCHA_HINTS):
        return REASONS["captcha"]
    if any(h in d for h in PAYMENT_HINTS):
        return REASONS["payment_frame"]
    return None


def text_reason(text: str) -> str | None:
    m = _SENSITIVE.search(normalize(text))
    return REASONS[m.lastgroup] if m else None


def classify(field: dict) -> str | None:
    """Return why this field must be left to the user, or None if Lantern may fill it."""
    ftype = (field.get("type") or "").lower()
    if ftype == "password":
        return REASONS["password"]
    tokens = (field.get("autocomplete") or "").lower().split()
    if "one-time-code" in tokens:
        return REASONS["otp"]
    if any(t.startswith("cc-") for t in tokens):
        return REASONS["card"]
    if any(t in ("current-password", "new-password") for t in tokens):
        return REASONS["password"]
    if field.get("frame"):
        reason = frame_reason(field["frame"])
        if reason:
            return reason
    if field.get("captcha"):
        return REASONS["captcha"]
    words = " ".join(str(field.get(k) or "") for k in ("label", "name", "html_id", "id", "placeholder", "aria_label"))
    return text_reason(words)


def value_reason(text: str) -> str | None:
    """Refuse values that look like a card number, an ID number or a secret."""
    found = SCRUBBER.scrub(text or "").redactions
    for kind in found:
        return f"the text looks like {SCRUB_REASONS.get(kind, 'a secret')}"
    return None


def is_captcha(reason: str | None) -> bool:
    return reason == REASONS["captcha"]
