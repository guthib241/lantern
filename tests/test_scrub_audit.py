"""M3: scrubber + audit. Planted fake keys must never reach the (fake) cloud."""
import json

import pytest

from fakes import FakeClient, call
from lantern.agent import Agent
from lantern.audit import AuditLog, GuardedClient, canonical_bytes, sha256_hex
from lantern.memory import Memory
from lantern.scrubber import Scrubber, luhn_ok, scrub, verhoeff_ok
from lantern.tools import Toolbox

# Fake secrets, assembled at runtime so the repo never contains a scannable token.
FAKE_AWS = "AKIA" + "Z" * 12 + "7Q2X"
FAKE_GH = "ghp_" + "a1B2" * 9
FAKE_STRIPE = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
FAKE_META = "LLM|" + "123456789|" + "abcdefghijklmnop"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
AADHAAR = "2341 2341 2346"   # passes Verhoeff
CARD = "4111 1111 1111 1111"  # Luhn-valid test card


def test_checksums():
    assert luhn_ok(CARD) and not luhn_ok("4111 1111 1111 1112")
    assert verhoeff_ok(AADHAAR) and not verhoeff_ok("2341 2341 2345")


@pytest.mark.parametrize("secret,kind", [
    (FAKE_AWS, "aws_access_key"), (FAKE_GH, "github_token"), (FAKE_STRIPE, "stripe_key"),
    (FAKE_META, "meta_model_key"), (FAKE_JWT, "jwt"), (PEM, "private_key"),
    ("sk-" + "x" * 32, "api_key"), ("AIza" + "B" * 35, "google_api_key"),
])
def test_secrets_redacted(secret, kind):
    r = scrub(f"config: {secret} end")
    assert secret not in r.text and f"[REDACTED:{kind}]" in r.text and r.redactions[kind] == 1


def test_assignments_and_urls_keep_context():
    r = scrub('password = "hunter2hunter"\nDB=postgres://bob:s3cretpw@db:5432/x\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz123')
    assert "hunter2hunter" not in r.text and "password = " in r.text
    assert "s3cretpw" not in r.text and "postgres://bob:" in r.text
    assert "abcdefghijklmnopqrstuvwxyz123" not in r.text


def test_indian_ids():
    r = scrub(f"Aadhaar {AADHAAR}, PAN ABCPE1234F, pay me at priya.k@okaxis")
    assert AADHAAR not in r.text and "ABCPE1234F" not in r.text and "priya.k@okaxis" not in r.text
    assert set(r.redactions) == {"aadhaar", "pan", "upi_id"}


def test_card_needs_luhn_and_plain_numbers_survive():
    r = scrub(f"card {CARD}; order 1234 5678 9012 3456; invoice #42 total 1200; phone 9876543210")
    assert CARD not in r.text and "1234 5678 9012 3456" in r.text
    assert "1200" in r.text and "9876543210" in r.text  # phones kept by default


def test_emails_kept_by_default_optional_scrub():
    assert "priya@example.com" in scrub("mail priya@example.com").text
    assert "priya@example.com" not in Scrubber(emails=True).scrub("mail priya@example.com").text


def test_scrub_is_idempotent_and_nested():
    s = Scrubber()
    once = s.scrub(f"k={FAKE_AWS}").text
    assert s.scrub(once).text == once
    obj, found = s.scrub_obj({"a": [FAKE_GH, {"b": CARD}], "n": 3})
    assert FAKE_GH not in json.dumps(obj) and found["github_token"] == 1 and obj["n"] == 3


def test_guarded_client_scrubs_and_audits(tmp_path):
    audit = AuditLog(tmp_path / "audit.db")
    fake = FakeClient([([], "ok")])
    g = GuardedClient(fake, audit)
    g.responses.create(model="muse-spark-1.3", instructions="be nice", input=f"my key is {FAKE_AWS}",
                       tools=[{"type": "function", "name": "x"}])
    sent = fake.requests[0]
    assert FAKE_AWS not in json.dumps(sent) and "[REDACTED:aws_access_key]" in sent["input"]
    assert sent["tools"] == [{"type": "function", "name": "x"}]  # tool list untouched (cache-stable)
    e = audit.entries()[0]
    assert e["sha256"] == sha256_hex(canonical_bytes(sent)) and e["bytes"] == len(canonical_bytes(sent))
    assert e["redactions"] == {"aws_access_key": 1} and e["lane"] == "cloud"
    s = audit.summary()
    assert s["cloud"]["calls"] == 1 and s["cloud"]["redactions"]["aws_access_key"] == 1


def test_guarded_client_blocks_contributor(tmp_path):
    from lantern.models import ModelPolicyError
    g = GuardedClient(FakeClient([([], "")]), AuditLog(tmp_path / "a.db"))
    with pytest.raises(ModelPolicyError):
        g.responses.create(model="muse-spark-1.3-contributor", input="hi")


def test_local_lane_not_scrubbed_but_logged(tmp_path):
    from types import SimpleNamespace as NS
    seen = []
    chat = NS(completions=NS(create=lambda **kw: seen.append(kw) or "r"))
    audit = AuditLog(tmp_path / "a.db")
    g = GuardedClient(NS(chat=chat), audit, lane="local", destination="http://127.0.0.1:8080/v1")
    g.chat.completions.create(model="muse-glimmer", messages=[{"role": "user", "content": f"Aadhaar {AADHAAR}"}])
    assert AADHAAR in seen[0]["messages"][0]["content"]
    s = audit.summary()
    assert s["local"]["calls"] == 1 and s["cloud"]["bytes"] == 0


def test_planted_key_in_file_never_reaches_cloud(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "settings.txt").write_text(f"stripe = {FAKE_STRIPE}\nuser: priya")
    mem = Memory(tmp_path / "m.db")
    audit = AuditLog(tmp_path / "audit.db")
    fake = FakeClient([
        ([call("read_file", "c1", path=str(root / "settings.txt"))], ""),
        ([], "Your settings mention a Stripe key, which I redacted."),
    ])
    r = Agent(GuardedClient(fake, audit), mem, Toolbox([root], mem, lambda d: True)).run("what's in settings?")
    assert FAKE_STRIPE not in json.dumps(fake.requests, default=str)
    assert "[REDACTED:stripe_key]" in fake.requests[1]["input"][0]["output"]
    rows = audit.entries()
    assert len(rows) == 2 and all(row["task_id"] == r.task_id for row in rows)
    assert audit.summary()["cloud"]["redactions"] == {"stripe_key": 1}


def test_secret_at_line_start_inside_json_tool_output_is_caught():
    # Tool results travel as JSON strings, so a key at the start of a line follows a literal "\n"
    # and \b never matches on the raw text. The decoded values must be scrubbed.
    s = Scrubber()
    output = json.dumps({"content": f"notes:\n{FAKE_AWS}\nend", "path": "n.md"}, ensure_ascii=False)
    assert s.scrub(output).count == 0  # the gap this guards against
    obj, found = s.scrub_obj([{"type": "function_call_output", "call_id": "c1", "output": output}])
    assert FAKE_AWS not in json.dumps(obj) and found["aws_access_key"] == 1
    assert json.loads(obj[0]["output"])["content"] == "notes:\n[REDACTED:aws_access_key]\nend"
    clean = json.dumps({"content": "nothing secret\nhere"})
    assert s.scrub_obj([{"output": clean}])[0][0]["output"] == clean  # untouched, byte for byte
    assert s.scrub_obj("[not json")[0] == "[not json"
    keyed = json.dumps({FAKE_GH: "x", "k": f"\n{FAKE_AWS}"})  # a secret used as a key is caught too
    out, found = s.scrub_obj(keyed)
    assert FAKE_GH not in out and FAKE_AWS not in out and found == {"aws_access_key": 1, "github_token": 1}


def test_wire_ids_are_never_redacted(tmp_path):
    # A call_id that happens to be a Luhn-valid digit run must survive, or the chain breaks.
    cid = "call_" + CARD.replace(" ", "")
    fake = FakeClient([([], "ok")])
    GuardedClient(fake, AuditLog(tmp_path / "a.db")).responses.create(
        model="muse-spark-1.3", previous_response_id="resp_1",
        input=[{"type": "function_call_output", "call_id": cid,
                "output": json.dumps({"card": CARD, "id": FAKE_AWS})}])
    item = fake.requests[0]["input"][0]
    assert item["call_id"] == cid and item["type"] == "function_call_output"
    assert CARD not in item["output"] and FAKE_AWS not in item["output"]  # nested ids still scrubbed
