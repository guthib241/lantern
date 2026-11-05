"""A Telegram bot token lives in the Bot API's URL path (/bot<token>/sendMessage). A failed call used to put that
URL in its error, and errors reach logs, task notes and the model. Found in the branch audit (Nov 5)."""
import pytest

from lantern.channels.base import ChannelError, json_call, multipart_call, safe_url
from lantern.channels.telegram import TelegramChannel
from lantern.scrubber import scrub

TOKEN = "123456789:AA" + "x" * 33   # the Bot API shape: <bot id>:<35 characters>; made up for the test


def refusing(status=400, body=b'{"ok": false, "description": "Bad Request: chat not found"}'):
    def http(method, url, headers, body_=None):
        return status, body
    return http


def test_a_failed_bot_api_call_never_quotes_the_token():
    for call in (lambda: json_call(refusing(), "POST", f"https://api.telegram.org/bot{TOKEN}/sendMessage", {}),
                 lambda: multipart_call(refusing(), f"https://api.telegram.org/bot{TOKEN}/sendPhoto", {},
                                        "photo", "a.png", b"x", "image/png")):
        with pytest.raises(ChannelError) as e:
            call()
        assert TOKEN not in str(e.value) and "AA" + "x" * 33 not in str(e.value)
        assert "/bot<token>/" in str(e.value) and "[400]" in str(e.value)   # still says what failed


def test_the_channel_itself_reports_failures_without_the_token():
    tg = TelegramChannel(TOKEN, "4242", http=refusing(status=500))
    with pytest.raises(ChannelError) as e:
        tg.send_file(b"x", "f.bin")
    assert TOKEN not in str(e.value) and "sendDocument" in str(e.value)


def test_safe_url_keeps_what_helps_and_drops_what_leaks():
    assert safe_url(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset=5") == \
        "https://api.telegram.org/bot<token>/getUpdates"
    assert safe_url("https://graph.facebook.com/v21.0/123/messages?access_token=abc") == \
        "https://graph.facebook.com/v21.0/123/messages"


def test_a_bot_token_in_outgoing_text_is_redacted():
    r = scrub(f"my bot token is {TOKEN}, keep it safe")
    assert TOKEN not in r.text and r.redactions["telegram_bot_token"] == 1
    assert scrub("call me at 9876543210 or 12:30").text == "call me at 9876543210 or 12:30"   # no false alarm
