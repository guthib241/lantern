"""S14 on Modal: base Muse Glimmer vs a LoRA-tuned one on the narrow job of choosing a learned routine.

    modal run scripts/s14_weights.py                 # first, once: weights → a Modal Volume, on a CPU box
    modal run scripts/s14_modal.py --step baseline   # base Glimmer on the held-out set (1 × H100)
    modal run scripts/s14_modal.py --step train      # QLoRA on train.jsonl (1 × H100) — only after the owner's OK
    modal run scripts/s14_modal.py --step tuned      # the same held-out eval with the adapter

Sources: dev.meta.ai/docs/muse-glimmer (get-the-model: `meta-models/Muse-Glimmer-30B`, Apache 2.0, ungated; deploy:
the `vllm/vllm-openai:muse-glimmer` image, ~72 GB to serve; post-training: the TRL + PEFT QLoRA recipe and its
hyperparameter ranges).
What leaves this computer: only the rows of demo/s14/data/{train,heldout}.jsonl (scrubbed by s14_export.py),
passed as function arguments. The weights come from Hugging Face on Modal's side. Results come back to
demo/s14/data/eval_<name>.json.
Both evals use the same engine and settings (vLLM, greedy, the same prompt Lantern sends); an answer counts when
the JSON's "routine" equals the label exactly.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import modal

VOL = "/vol"
MODEL_DIR = f"{VOL}/Muse-Glimmer-30B"
ADAPTERS = f"{VOL}/adapters"
GPU = os.environ.get("LANTERN_S14_GPU", "A100-80GB")   # 80 GB: the ~72 GB vLLM needs, and the QLoRA run
MAX_LEN = 8192          # the prompts are ~1K tokens; a short context keeps the KV cache small on one card
MAX_NEW = 2048          # Glimmer reasons before it answers; room for both

app = modal.App("lantern-s14")
volume = modal.Volume.from_name("lantern-glimmer", create_if_missing=True)
vllm_image = modal.Image.from_registry("vllm/vllm-openai:muse-glimmer").entrypoint([])
train_image = (modal.Image.debian_slim(python_version="3.12")
               .pip_install("torch>=2.8", "transformers>=5.15", "peft>=0.17", "trl>=0.24", "bitsandbytes>=0.47",
                            "accelerate>=1.10", "datasets>=4"))


def routine_of(text: str) -> str:
    """The "routine" of the last JSON object in the answer ("?" when there is none)."""
    for m in reversed(list(re.finditer(r"\{[^{}]*\"routine\"[^{}]*\}", text or ""))):
        try:
            return str(json.loads(m.group(0)).get("routine", "?")).strip() or "?"
        except ValueError:
            continue
    return "?"


@app.function(image=vllm_image, gpu=GPU, volumes={VOL: volume}, timeout=3600)
def evaluate(rows: list[dict], adapter: str = "") -> dict:
    from vllm import LLM, SamplingParams
    t0 = time.time()
    kw = {"enable_lora": True, "max_lora_rank": 32} if adapter else {}
    llm = LLM(MODEL_DIR, max_model_len=MAX_LEN, gpu_memory_utilization=0.92, **kw)
    loaded = time.time()
    lora = None
    if adapter:
        from vllm.lora.request import LoRARequest
        lora = LoRARequest("s14", 1, f"{ADAPTERS}/{adapter}")
    outs = llm.chat([r["messages"][:2] for r in rows], SamplingParams(temperature=0.0, max_tokens=MAX_NEW),
                    lora_request=lora)
    done = time.time()
    preds = [{"expected": r["expected"], "got": routine_of(o.outputs[0].text),
              "tokens": len(o.outputs[0].token_ids), "finish": o.outputs[0].finish_reason} for r, o in zip(rows, outs)]
    return {"adapter": adapter or None, "correct": sum(p["got"] == p["expected"] for p in preds), "of": len(rows),
            "none_correct": sum(p["got"] == p["expected"] == "none" for p in preds),
            "none_of": sum(p["expected"] == "none" for p in preds),
            "unreadable": sum(p["got"] == "?" for p in preds),
            "seconds": {"load": round(loaded - t0), "generate": round(done - loaded)}, "predictions": preds}


@app.function(image=train_image, gpu=GPU, volumes={VOL: volume}, timeout=2 * 3600)
def train(rows: list[dict], name: str, epochs: int = 2, rank: int = 16, lr: float = 2e-4) -> dict:
    """The post-training page's TRL QLoRA recipe: 4-bit NF4 base, LoRA on q/k/v/o, alpha = 2 × rank."""
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer
    t0 = time.time()
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_use_double_quant=True, bnb_4bit_quant_storage=torch.bfloat16)
    model = AutoModelForCausalLM.from_pretrained(MODEL_DIR, quantization_config=bnb, device_map="auto",
                                                 dtype=torch.bfloat16)
    model = prepare_model_for_kbit_training(model)
    tok = AutoTokenizer.from_pretrained(MODEL_DIR)
    model = get_peft_model(model, LoraConfig(r=rank, lora_alpha=2 * rank, lora_dropout=0.05, task_type="CAUSAL_LM",
                                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]))
    loaded = time.time()
    out = f"{ADAPTERS}/{name}"
    trainer = SFTTrainer(model=model, processing_class=tok,
                         train_dataset=Dataset.from_list([{"messages": r["messages"]} for r in rows]),
                         args=SFTConfig(output_dir=f"/tmp/{name}", num_train_epochs=epochs, learning_rate=lr,
                                        per_device_train_batch_size=1, gradient_accumulation_steps=16, bf16=True,
                                        logging_steps=2, save_strategy="no", max_length=2048, report_to=[],
                                        assistant_only_loss=False, seed=14))
    result = trainer.train()
    trainer.save_model(out)
    volume.commit()
    losses = [h["loss"] for h in trainer.state.log_history if "loss" in h]
    return {"adapter": name, "examples": len(rows), "epochs": epochs, "rank": rank, "lr": lr,
            "steps": result.global_step, "loss_first": losses[:1], "loss_last": losses[-1:],
            "seconds": {"load": round(loaded - t0), "train": round(time.time() - loaded)}}


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@app.local_entrypoint()
def main(step: str, data: str = "demo/s14/data", adapter: str = "s14-routine-choice", epochs: int = 2):
    d = Path(data)
    if step in ("baseline", "tuned"):
        res = evaluate.remote(_rows(d / "heldout.jsonl"), adapter if step == "tuned" else "")
        (d / f"eval_{step}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        print({k: v for k, v in res.items() if k != "predictions"})
    elif step == "train":
        res = train.remote(_rows(d / "train.jsonl"), adapter, epochs=epochs)
        (d / "train_result.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(res)
    else:
        raise SystemExit("step: baseline | train | tuned (weights: scripts/s14_weights.py)")
