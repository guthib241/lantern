"""Muse Spark Open on Baseten (lantern/baseten.py). Scripted Chat Completions fakes only; never the real API."""
import base64
from types import SimpleNamespace as NS

import pytest

from lantern import baseten, models
from lantern.audit import AuditLog, EgressBlocked
from lantern.policy import PRIVATE, Policy, PolicyError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
ON = {"LANTERN_BASETEN": "on", "BASETEN_API_KEY": "injected-by-proxy"}


class FakeChat:
    """Scripted chat.completions: items are (content, reasoning, finish, usage) or an exception to raise."""

    def __init__(self, *script):
        self.script, self.requests = list(script), []
        self.chat = NS(completions=self)

    def create(self, **kw):
        self.requests.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        content, reasoning, finish, u = item
        return NS(choices=[NS(message=NS(role="assistant", content=content, reasoning_content=reasoning),
                              finish_reason=finish)], usage=u)


def chat_usage(prompt, completion, reasoning=0):
    return NS(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion,
              prompt_tokens_details=None, completion_tokens_details=NS(reasoning_tokens=reasoning))


class Ledger:
    def __init__(self):
        self.rows = []

    def log_usage(self, *a):
        self.rows.append(a)


def lane(fake, **kw):
    return baseten.SparkOpen(baseten.BasetenConfig.from_env(ON), client=fake, **kw)


@pytest.mark.parametrize("env", [{}, {"BASETEN_API_KEY": "k"}, {"LANTERN_BASETEN": "on"},
                                 {"LANTERN_BASETEN": "off", "BASETEN_API_KEY": "k", "LANTERN_BASETEN_MODEL": "x"}])
def test_off_by_default_and_a_missing_key_never_breaks_anything(env):
    assert baseten.from_env(env) is None   # no error: every other path carries on as before


def test_only_muse_spark_open_is_accepted():
    with pytest.raises(baseten.BasetenConfigError):
        baseten.BasetenConfig.from_env({**ON, "LANTERN_BASETEN_MODEL": "meta-models/some-other-model"})
    with pytest.raises(baseten.BasetenConfigError):
        baseten.BasetenConfig(model=models.SPARK)
    with pytest.raises(baseten.BasetenConfigError):
        baseten.BasetenConfig.from_env({**ON, "LANTERN_BASETEN_EFFORT": "max"})
    assert baseten.BasetenConfig.from_env(ON).model == "meta-models/muse-spark-open-1026"


def test_the_key_is_never_in_the_config_repr():
    assert "secret-value" not in repr(baseten.BasetenConfig(enabled=True, api_key="secret-value"))


def test_off_or_keyless_lane_refuses_to_start():
    with pytest.raises(baseten.BasetenError, match="off"):
        baseten.SparkOpen(baseten.BasetenConfig(), client=FakeChat())
    with pytest.raises(baseten.BasetenError, match="BASETEN_API_KEY"):
        baseten.SparkOpen(baseten.BasetenConfig(enabled=True), client=FakeChat())


def test_reasoning_is_dropped_and_its_tokens_are_paid_for():
    fake = FakeChat(("391", "17*23: 17*20=340, 17*3=51, 340+51=391 ...", "stop", chat_usage(40, 900, reasoning=880)))
    ledger = Ledger()
    r = lane(fake, memory=ledger).ask("Answer with the number only.", "What is 17*23?", task_id=7)
    assert r.text == "391" and "340" not in r.text
    assert r.usage.output_tokens == 900 and r.usage.reasoning_tokens == 880
    assert r.cost_usd == pytest.approx((40 * 1.25 + 900 * 4.25) / 1e6)
    assert ledger.rows == [(7, models.SPARK_OPEN_BASETEN, 40, 0, 900, r.cost_usd)]
    kw = fake.requests[0]
    assert kw["model"] == models.SPARK_OPEN_BASETEN and kw["reasoning_effort"] == "low" and kw["max_tokens"] == 4096
    assert kw["messages"][0] == {"role": "system", "content": "Answer with the number only."}


def test_inline_thinking_is_cut_from_the_answer():
    fake = FakeChat(("<think>secret plan</think>\nThe bill is due on 3 Nov.", None, "stop", chat_usage(5, 5)))
    assert lane(fake).ask("s", "q").text == "The bill is due on 3 Nov."


def test_no_answer_after_reasoning_is_an_error_not_an_empty_reply():
    fake = FakeChat(("", "thinking...", "length", chat_usage(10, 4096)), ("  ", None, "stop", chat_usage(1, 1)))
    with pytest.raises(baseten.BasetenError, match="max_tokens"):
        lane(fake).ask("s", "q")
    with pytest.raises(baseten.BasetenError, match="empty"):
        lane(fake).ask("s", "q")


def test_api_errors_become_a_plain_lane_error():
    fake = FakeChat(RuntimeError("403 please check the api-key you provided"))
    with pytest.raises(baseten.BasetenError, match="didn't answer"):
        lane(fake).ask("s", "q")


def test_one_image_goes_as_a_data_url_part():
    fake = FakeChat(("A receipt.", None, "stop", chat_usage(300, 20)))
    lane(fake).ask("Describe it in one line.", "What is this?", images=[PNG])
    content = fake.requests[0]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "What is this?"}
    part = content[1]
    assert part["type"] == "image_url" and part["image_url"]["detail"] == "auto"
    assert part["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()


def test_images_are_checked_before_anything_is_sent():
    fake = FakeChat()
    with pytest.raises(baseten.BasetenError, match="not an image"):
        lane(fake).ask("s", "q", images=[b"%PDF-1.7 ..."])
    with pytest.raises(baseten.BasetenError, match="10 MB"):
        lane(fake).ask("s", "q", images=[PNG + b"\x00" * baseten.MAX_IMAGE_BYTES])
    with pytest.raises(baseten.BasetenError, match="at most 8"):
        lane(fake).ask("s", "q", images=[PNG] * 9)
    assert fake.requests == []
    assert baseten.image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"


def test_private_files_never_reach_baseten(tmp_path):
    (tmp_path / "Private").mkdir()
    (tmp_path / "Pictures").mkdir()
    (tmp_path / "Private" / "passport.png").write_bytes(PNG)
    (tmp_path / "Pictures" / "receipt.png").write_bytes(PNG)
    policy = Policy.from_roots([tmp_path])
    policy.set_zone(tmp_path / "Private", PRIVATE)
    with pytest.raises(PolicyError, match="private"):
        baseten.read_image(policy, tmp_path / "Private" / "passport.png")
    assert baseten.read_image(policy, tmp_path / "Pictures" / "receipt.png") == PNG


def test_requests_are_scrubbed_audited_and_blocked_when_they_carry_private_values(tmp_path):
    audit = AuditLog(tmp_path / "audit.db")
    fake = FakeChat(("ok", None, "stop", chat_usage(10, 2)))
    sp = lane(fake, audit=audit, deny=lambda: ["Rahul Verma passport Z1234567"])
    sp.ask("s", "Summarise: AWS key AKIAIOSFODNN7EXAMPLE in the readme", task_id=3)
    sent = fake.requests[0]["messages"][1]["content"]
    assert "AKIAIOSFODNN7EXAMPLE" not in sent                       # scrubbed on the cloud lane
    row = audit.for_task(3)[0]
    assert row["destination"] == models.BASETEN_BASE_URL and row["lane"] == "cloud"
    with pytest.raises(EgressBlocked):
        sp.ask("s", "Fill in Rahul Verma passport Z1234567 please", task_id=3)
    assert len(fake.requests) == 1                                  # nothing sent the second time


def test_contributor_models_stay_refused():
    with pytest.raises(baseten.BasetenConfigError):
        baseten.BasetenConfig(model="meta-models/muse-spark-open-1026-contributor")
