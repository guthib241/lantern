"""deploy/modal/glimmer_config.py: deploy settings, the llama-server command, cost math. No Modal, no network."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "modal"))
import glimmer_config as gc  # noqa: E402


def test_defaults_fit_a_24gb_gpu():
    cfg = gc.ServeConfig()
    assert (cfg.gpu, cfg.quant, cfg.context) == ("A10", "q4_k_m", 16384)
    for gpu in ("A10", "L4"):   # A10 is the default (measured); L4 stays an option
        assert gc.vram_needed_gib(cfg.quant, cfg.context, cfg.draft) < gc.GPU_VRAM_GIB[gpu]
        assert gc.ServeConfig(gpu=gpu).gpu == gpu


def test_kv_cache_is_small_because_most_layers_are_sliding_window():
    # 13 global layers grow with context, 39 local layers stop at the 2048 window
    assert gc.kv_cache_gib(16384) == pytest.approx(0.28, abs=0.01)
    assert gc.kv_cache_gib(32768) - gc.kv_cache_gib(16384) == pytest.approx(13 * 16384 * 1024 / 2**30)


def test_rejects_setups_that_cannot_work():
    with pytest.raises(gc.ConfigError, match="needs"):
        gc.ServeConfig(gpu="T4")                      # 15 GiB: the 16.8 GB file alone doesn't fit
    with pytest.raises(gc.ConfigError, match="unknown GPU"):
        gc.ServeConfig(gpu="RTX-4060")
    with pytest.raises(gc.ConfigError, match="unknown quant"):
        gc.ServeConfig(quant="bf16")
    with pytest.raises(gc.ConfigError, match="idle"):
        gc.ServeConfig(idle_seconds=7200)             # must scale to zero quickly
    with pytest.raises(gc.ConfigError, match="reasoning"):
        gc.ServeConfig(reasoning_strength="none")


def test_bigger_quant_needs_a_bigger_gpu_when_the_drafter_is_on():
    gc.ServeConfig(gpu="L40S", quant="q4_k_xl")
    gc.ServeConfig(gpu="L4", quant="q4_k_xl", draft=False)
    with pytest.raises(gc.ConfigError):
        gc.ServeConfig(gpu="L4", quant="q4_k_xl", draft=True, context=131072)


def test_from_env_round_trips_through_as_env():
    cfg = gc.ServeConfig.from_env({"LANTERN_GLIMMER_GPU": "A10", "LANTERN_GLIMMER_DRAFT": "0",
                                   "LANTERN_GLIMMER_CTX": "8192", "LANTERN_GLIMMER_IDLE": "120"})
    assert (cfg.gpu, cfg.draft, cfg.context, cfg.idle_seconds) == ("A10", False, 8192, 120)
    assert gc.ServeConfig.from_env(cfg.as_env()) == cfg
    assert gc.ServeConfig.from_env({}) == gc.ServeConfig()


def test_from_env_rejects_non_numbers():
    with pytest.raises(gc.ConfigError, match="whole number"):
        gc.ServeConfig.from_env({"LANTERN_GLIMMER_CTX": "16k"})


def test_llama_server_command_follows_the_glimmer_deploy_docs():
    cmd = gc.llama_server_cmd(gc.ServeConfig())
    joined = " ".join(cmd)
    assert cmd[0] == gc.LLAMA_SERVER
    assert "--host 127.0.0.1" in joined                     # never reachable except through the gate
    assert "-a muse-glimmer" in joined and "--jinja" in joined
    assert "-c 16384 -np 1" in joined                       # one slot gets the whole context
    assert "--temp 1.0 --top-p 0.95 --top-k 64" in joined   # never temperature 0
    assert '{"reasoning_strength":"low"}' in cmd
    assert f"/models/{gc.WEIGHTS['q4_k_m'].filename}" in cmd
    assert "-md" in cmd and f"/models/{gc.DRAFT.filename}" in cmd
    assert "-v" not in cmd and "--verbose" not in cmd and "--verbose-prompt" not in cmd   # no content in logs
    assert "--api-key" not in joined                          # the token lives only in the gate
    assert "-md" not in gc.llama_server_cmd(gc.ServeConfig(draft=False))


def test_llama_image_is_pinned_and_new_enough_for_glimmer():
    tag = gc.LLAMA_IMAGE.rsplit(":", 1)[1]
    assert tag.startswith("server-cuda-b")
    assert int(tag.rsplit("-b", 1)[1]) >= 10353


def test_weights_are_pinned():
    assert len(gc.HF_REVISION) == 40
    for f in [*gc.WEIGHTS.values(), gc.DRAFT]:
        assert len(f.sha256) == 64 and f.size_bytes > 10**9 and f.filename.endswith(".gguf")


def test_cost_estimate():
    cfg = gc.ServeConfig()          # A10 1.10 + 1 core 0.0473 + 4 GiB 0.032
    assert cfg.price_per_hour() == pytest.approx(1.1793)
    assert gc.cost_usd(3600, cfg) == pytest.approx(1.1793)
    assert gc.cost_usd(300, cfg) == pytest.approx(1.1793 / 12)
    assert gc.ServeConfig(gpu="L4").price_per_hour() == pytest.approx(0.8793)


@pytest.mark.parametrize("token,ok", [
    ("a" * 43, True), ("Ab3_-" * 9, True),
    (None, False), ("", False), ("short", False), ("x" * 31, False),
    ("has space in it" * 3, False), ("ünïcode" * 6, False), ("a" * 257, False),
])
def test_validate_token(token, ok):
    if ok:
        assert gc.validate_token(token) == token
    else:
        with pytest.raises(gc.ConfigError):
            gc.validate_token(token)


def test_default_names():
    assert gc.names_from_env({}) == ("lantern-glimmer", "lantern-private", "~/.lantern/private_token")


def test_separate_names_get_their_own_token_file():
    app, secret, token_file = gc.names_from_env({"LANTERN_GLIMMER_APP": "lantern-glimmer-qa",
                                                 "LANTERN_GLIMMER_SECRET": "lantern-private-qa"})
    assert (app, secret) == ("lantern-glimmer-qa", "lantern-private-qa")
    assert token_file == "~/.lantern/lantern-private-qa_token"   # never overwrites the default token file


@pytest.mark.parametrize("bad", ["has space", "../x", "a" * 65, "-leading", "semi;colon"])
def test_names_are_validated(bad):
    with pytest.raises(ValueError):
        gc.names_from_env({"LANTERN_GLIMMER_APP": bad})
    with pytest.raises(ValueError):
        gc.names_from_env({"LANTERN_GLIMMER_SECRET": bad})


def test_cpu_mode_runs_without_a_gpu_and_without_the_drafter():
    """Live, Oct 28: Modal refuses every GPU until a card is on file; CPU functions run on credit alone."""
    c = gc.ServeConfig.from_env({"LANTERN_GLIMMER_GPU": "CPU"})
    assert c.cpu_only and not c.draft and c.resources() == (gc.CPU_CORES, gc.CPU_MEMORY_MIB)
    cmd = gc.llama_server_cmd(c)
    assert cmd[cmd.index("-ngl") + 1] == "0" and cmd[cmd.index("-t") + 1] == str(int(gc.CPU_CORES))
    assert "-md" not in cmd
    assert round(c.price_per_hour(), 2) == round(gc.CPU_CORES * gc.CPU_PER_CORE_HOUR
                                                 + gc.CPU_MEMORY_MIB / 1024 * gc.MEM_PER_GIB_HOUR, 2)
    with pytest.raises(gc.ConfigError):
        gc.ServeConfig(gpu="CPU", draft=True)


def test_no_mmap_is_spelled_for_this_llama_cpp_build():
    """Live, Oct 28: server-b11913 rejects --no-mmap ("invalid argument") and crash-looped; it is --load-mode none."""
    for c in (gc.ServeConfig(), gc.ServeConfig.from_env({"LANTERN_GLIMMER_GPU": "CPU"})):
        cmd = gc.llama_server_cmd(c)
        assert "--no-mmap" not in cmd and cmd[cmd.index("--load-mode") + 1] == "none"
