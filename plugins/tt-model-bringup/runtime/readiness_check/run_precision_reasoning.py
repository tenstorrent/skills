# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
Precision on reasoning text (eval-readiness probe P1, stage 08 datatype-sweep).

Short top-1/top-5 references (100 greedy tokens) can pass a compressed-weight
policy that still loses eval points on long reasoning answers. This probe
teacher-forces every precision candidate over the same long, chat-formatted
reasoning answers and compares it with a reference configuration position by
position.

Steps:

1. Text. ``--text-file`` (JSONL traces) or, by default, about 30 answers
   generated once by the reference configuration through the generator, with
   thinking on via the HF chat template and the model-card sampling
   (``generation_config.json``) when ``generate()`` declares sampling keywords
   or a ``sampling_params`` object.
   The token ids are cached in ``doc/eval_readiness/precision_reasoning/text.jsonl``
   so every candidate scores the same tokens.
2. Scoring. For each candidate (``--candidates``: id -> ``build_generator``
   kwargs), one ``prefill_logits`` call per text gives the logits at every
   answer position. Per position: NLL of the actual next token, argmax, and
   KL(reference || candidate) over the reference's top-``--topk`` tokens plus
   one tail bin (a coarse-grained lower bound of the full KL). Optional traced
   decode teacher forcing (``--decode-tokens``) covers decode-only numerics.
3. Statistics. Mean paired ΔNLL (candidate − reference) with a cluster-robust
   standard error: clusters are texts, so correlation inside a text is not
   counted as independent evidence; with fewer than 8 texts, contiguous
   128-position blocks are the clusters.
4. Optional HF slice (``--hf-positions``): the original HF model on the CPU
   scores the reference's first texts, to catch losses the reference itself
   has (a fast GELU approximation, for example).

Checks (``doc/eval_readiness/precision_reasoning.json``):

- ``positions_scored`` (critical): at least ``--min-positions`` paired positions.
- ``selected_within_noise`` (critical, waivable): the selected candidate's
  ΔNLL <= max(2·SE, ``--nll-floor``) and KL×1000 <= ``--kl-max-x1000``.
- ``decode_top1_within_noise`` (advisory): decode top-1 vs the text token is
  not lower than the reference's by more than max(2·SE, 0.01).
- ``reference_vs_hf`` (advisory): KL(reference || HF)×1000 <= ``--hf-kl-max-x1000``.

CLI:
    python -m readiness_check.run_precision_reasoning \\
        --model-dir models/autoports/<model> --hf-model <org/name> \\
        --mesh-device P300x2 --candidates candidates.json \\
        --reference bfp8 --selected bfp4_mlp
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import inspect
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import torch

PROBE = "precision_reasoning"
RAW_DIR = Path("doc/eval_readiness") / PROBE
TEXT_CACHE = RAW_DIR / "text.jsonl"
SCORES_DIR = RAW_DIR / "scores"
DEFAULT_PROMPTS = Path(__file__).parent / "reasoning_prompts.json"

DEFAULT_NUM_TEXTS = 30
DEFAULT_MAX_NEW_TOKENS = 6144
DEFAULT_MAX_TEXT_TOKENS = 8192
DEFAULT_MIN_ANSWER_TOKENS = 512
DEFAULT_TOPK = 64
DEFAULT_MIN_POSITIONS = 1000
# Absolute floor on a ΔNLL that counts as "equal": 0.01 nats/token. For text
# sampled from the reference at temperature 1, E[ΔNLL] = KL(ref || cand), so
# the floor is KL×1000 = 10. Evidence (Gemma-12B, KL×1000 vs HF on GPQA
# traces): BFP4 default 74, no-BFP4 28, no-BFP4 + exact GELU 7.7; GPQA moved
# 66.7 -> 69.4 -> 72.9.
DEFAULT_NLL_FLOOR = 0.01
DEFAULT_KL_MAX_X1000 = 15.0
DEFAULT_HF_KL_MAX_X1000 = 15.0
DEFAULT_HF_POSITIONS = 2048
DEFAULT_DECODE_TOKENS = 256
DEFAULT_DECODE_TEXTS = 4
DEFAULT_MAX_CANDIDATES = 5
DECODE_TOP1_FLOOR = 0.01
MIN_CLUSTERS = 8
BLOCK_POSITIONS = 128
LOGIT_CHUNK_ROWS = 512
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "seed")
DEFAULT_CHAT_TEMPLATE_KWARGS = {"enable_thinking": True, "thinking": True}
HF_HEADROOM_BYTES = 16 * 2**30


# --------------------------------------------------------------------------- text set


def load_prompts(path: Path) -> List[dict]:
    data = json.loads(Path(path).read_text())
    rows = data["prompts"] if isinstance(data, dict) else data
    return [r for r in rows if isinstance(r, dict) and r.get("messages")]


def text_sha(texts: Sequence[dict]) -> str:
    h = hashlib.sha256()
    for t in texts:
        h.update(json.dumps([int(t["prompt_len"]), [int(x) for x in t["token_ids"]]]).encode())
    return h.hexdigest()[:16]


def read_text_cache(path: Path) -> List[dict]:
    texts = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            texts.append(row)
    return texts


def write_text_cache(path: Path, texts: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(t) + "\n" for t in texts))


def is_degenerate(tokens: Sequence[int], window: int = 512, max_period: int = 64) -> bool:
    """True when the tail is a short loop: the last ``window`` tokens repeat with a period <= ``max_period``."""
    tail = list(tokens[-window:])
    if len(tail) < window:
        return False
    for period in range(1, max_period + 1):
        if all(tail[i] == tail[i - period] for i in range(period, len(tail))):
            return True
    eight = [tuple(tail[i : i + 8]) for i in range(len(tail) - 7)]
    return len(set(eight)) < 0.25 * len(eight)


def chat_prompt_ids(tokenizer: Any, messages: list, template_kwargs: dict) -> List[int]:
    ids = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=True, **template_kwargs)
    if isinstance(ids, dict) or hasattr(ids, "input_ids"):
        ids = ids["input_ids"]
    if ids and isinstance(ids[0], list):
        ids = ids[0]
    return [int(x) for x in ids]


def texts_from_file(path: Path, tokenizer: Any, template_kwargs: dict, max_text_tokens: int) -> List[dict]:
    """Source B. Each JSONL row is one of:
    {"prompt_token_ids": [...], "completion_token_ids": [...]},
    {"token_ids": [...], "prompt_len": n}, or
    {"messages": [...], "response": "<answer text, thinking included>"}."""
    texts = []
    for i, line in enumerate(Path(path).read_text().splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        if "prompt_token_ids" in row:
            prompt = [int(x) for x in row["prompt_token_ids"]]
            ids = prompt + [int(x) for x in row["completion_token_ids"]]
            plen = len(prompt)
        elif "token_ids" in row:
            ids, plen = [int(x) for x in row["token_ids"]], int(row["prompt_len"])
        else:
            if tokenizer is None:
                raise SystemExit(f"{path}:{i + 1}: text rows need --hf-model for the tokenizer")
            prompt = chat_prompt_ids(tokenizer, row["messages"], template_kwargs)
            answer = tokenizer(row["response"], add_special_tokens=False)["input_ids"]
            ids, plen = prompt + [int(x) for x in answer], len(prompt)
        texts.append(
            {"id": str(row.get("id", f"t{i + 1:02d}")), "prompt_len": plen, "token_ids": ids[:max_text_tokens],
             "source": f"file:{Path(path).name}"}
        )
    return texts


def generate_texts(
    generator: Any,
    tokenizer: Any,
    prompts: Sequence[dict],
    *,
    num_texts: int,
    max_new_tokens: int,
    sampling: dict,
    template_kwargs: dict,
    min_answer_tokens: int,
    max_text_tokens: int,
    log: Callable[[str], None] = print,
) -> List[dict]:
    """Source A: answers from the reference configuration through ``generate()``.

    Model-card sampling is passed only for keywords ``generate()`` declares
    (temperature, top_p, top_k, seed); otherwise the answer is greedy and
    loops are dropped by ``is_degenerate``.
    """
    params = inspect.signature(generator.generate).parameters
    declared = {k for k in SAMPLING_KEYS if k in params}
    use_object = not declared and "sampling_params" in params
    if use_object:
        declared = set(SAMPLING_KEYS)
    texts: List[dict] = []
    dropped = 0
    for i, prompt in enumerate(prompts):
        if len(texts) >= num_texts:
            break
        prompt_ids = chat_prompt_ids(tokenizer, prompt["messages"], template_kwargs)
        kwargs = {k: v for k, v in sampling.items() if k in declared}
        if "seed" in declared:
            kwargs["seed"] = int(sampling.get("seed", 1000)) + i
        if i:
            generator.reset()
        call_kwargs = {"sampling_params": make_sampling_params(generator, kwargs)} if use_object else kwargs
        out = generator.generate(prompt_ids, max_new_tokens, next_input=None, enable_trace=True, **call_kwargs)
        answer = [int(x) for x in (out.tolist() if hasattr(out, "tolist") else out)]
        if len(answer) < min_answer_tokens or is_degenerate(answer):
            dropped += 1
            continue
        texts.append(
            {"id": str(prompt.get("id", f"r{i + 1:02d}")), "prompt_len": len(prompt_ids),
             "token_ids": (prompt_ids + answer)[:max_text_tokens], "source": "generated",
             "sampling": kwargs if kwargs else "greedy"}
        )
    log(f"generated {len(texts)} texts ({dropped} dropped: shorter than {min_answer_tokens} tokens or looping); "
        f"sampling {'greedy (generate() declares no sampling keywords)' if not declared else sorted(declared)}")
    return texts


def make_sampling_params(generator: Any, values: dict) -> Any:
    """Build the generator's own ``SamplingParams`` (looked up in its module, then the
    tt-metal common one) from temperature/top_p/top_k/seed; a namespace as a last resort."""
    import dataclasses
    import importlib
    from types import SimpleNamespace

    cls = getattr(sys.modules.get(type(generator).__module__), "SamplingParams", None)
    if cls is None:
        try:
            cls = importlib.import_module("models.common.sampling.sampling_params").SamplingParams
        except Exception:  # noqa: BLE001
            cls = None
    if cls is None or not dataclasses.is_dataclass(cls):
        return SimpleNamespace(**values)
    fields = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in values.items() if k in fields})


# --------------------------------------------------------------------------- scoring math


def score_logits(
    logits: torch.Tensor,
    token_ids: Sequence[int],
    prompt_len: int,
    *,
    topk: int = DEFAULT_TOPK,
    ref_topk_idx: Optional[torch.Tensor] = None,
    ref_topk_logp: Optional[torch.Tensor] = None,
) -> Dict[str, torch.Tensor]:
    """Per answer position: NLL of the actual next token, argmax, and either the
    top-k (when scoring the reference) or the coarse KL(ref || this) (when given
    the reference's top-k).

    ``logits`` is ``[1, L, V]`` or ``[L, V]``; row ``i`` predicts token ``i+1``,
    so the answer rows are ``prompt_len-1 .. L-2``.
    """
    rows = logits[0] if logits.dim() == 3 else logits
    n_tok = len(token_ids)
    if rows.shape[0] < n_tok:
        raise ValueError(f"logits cover {rows.shape[0]} positions, the text has {n_tok}")
    start, end = prompt_len - 1, n_tok - 1
    targets = torch.tensor([int(t) for t in token_ids[start + 1 : end + 1]], dtype=torch.long)
    nll, argmax, tk_i, tk_l, kl = [], [], [], [], []
    for r0 in range(start, end, LOGIT_CHUNK_ROWS):
        r1 = min(r0 + LOGIT_CHUNK_ROWS, end)
        logp = torch.log_softmax(rows[r0:r1].to("cpu", torch.float32), dim=-1)
        tgt = targets[r0 - start : r1 - start]
        nll.append(-logp.gather(1, tgt[:, None])[:, 0])
        argmax.append(logp.argmax(dim=-1).to(torch.int32))
        if ref_topk_idx is None:
            vals, idx = logp.topk(min(topk, logp.shape[-1]), dim=-1)
            tk_i.append(idx.to(torch.int32))
            tk_l.append(vals)
        else:
            ri = ref_topk_idx[r0 - start : r1 - start].long()
            rl = ref_topk_logp[r0 - start : r1 - start].float()
            kl.append(coarse_kl(rl, logp.gather(1, ri)))
    out = {"nll": torch.cat(nll), "argmax": torch.cat(argmax), "target": targets.to(torch.int32)}
    if ref_topk_idx is None:
        out["topk_idx"], out["topk_logp"] = torch.cat(tk_i), torch.cat(tk_l)
    else:
        out["kl"] = torch.cat(kl)
    return out


def coarse_kl(ref_logp: torch.Tensor, cand_logp: torch.Tensor) -> torch.Tensor:
    """KL(P || Q) over the reference's top-k tokens plus one tail bin, per row.

    Coarse-graining never increases KL, so this is a lower bound of the full
    KL; with k=64 the tail mass is usually small and the bound tight.
    """
    p = ref_logp.exp()
    head = (p * (ref_logp - cand_logp)).sum(-1)
    tail_p = (1.0 - p.sum(-1)).clamp_min(1e-12)
    tail_q = (1.0 - cand_logp.exp().sum(-1)).clamp_min(1e-12)
    return (head + tail_p * (tail_p.log() - tail_q.log())).clamp_min(0.0)


def clusters_for(lengths: Sequence[int]) -> List[slice]:
    """Texts are the clusters; with fewer than MIN_CLUSTERS texts, 128-position blocks."""
    out, pos = [], 0
    block = len(lengths) < MIN_CLUSTERS
    for n in lengths:
        if block:
            out += [slice(pos + b, pos + min(b + BLOCK_POSITIONS, n)) for b in range(0, n, BLOCK_POSITIONS)]
        else:
            out.append(slice(pos, pos + n))
        pos += n
    return out


def mean_se(values: torch.Tensor, clusters: Sequence[slice]) -> tuple[float, float]:
    """Mean and cluster-robust standard error (CR1) of per-position values."""
    v = values.double()
    n = v.numel()
    if n == 0:
        return float("nan"), float("nan")
    mean = float(v.mean())
    g = len(clusters)
    if g < 2:
        return mean, float("nan")
    sums = torch.stack([(v[c] - mean).sum() for c in clusters])
    return mean, math.sqrt(g / (g - 1) * float((sums**2).sum())) / n


def compare(ref: List[Dict[str, torch.Tensor]], cand: List[Dict[str, torch.Tensor]]) -> Dict[str, Any]:
    """Paired statistics of one candidate against the reference over the same texts."""
    k = min(len(ref), len(cand))
    ref, cand = ref[:k], cand[:k]
    lengths = [int(r["nll"].numel()) for r in ref]
    clusters = clusters_for(lengths)
    cat = lambda rows, key: torch.cat([r[key] for r in rows])  # noqa: E731
    d_nll = cat(cand, "nll").double() - cat(ref, "nll").double()
    agree = (cat(cand, "argmax") == cat(ref, "argmax")).double()
    target = cat(ref, "target")
    hit_c = (cat(cand, "argmax") == target).double()
    hit_r = (cat(ref, "argmax") == target).double()
    dnll, dnll_se = mean_se(d_nll, clusters)
    dtop1, dtop1_se = mean_se(hit_c - hit_r, clusters)
    out = {
        "texts": k,
        "positions": int(d_nll.numel()),
        "se_method": "cluster-robust over texts" if len(lengths) >= MIN_CLUSTERS else f"cluster-robust over {BLOCK_POSITIONS}-position blocks",
        "dnll": dnll,
        "dnll_se": dnll_se,
        "nll_ref": float(cat(ref, "nll").double().mean()),
        "top1_agree": float(agree.mean()),
        "top1_text": float(hit_c.mean()),
        "dtop1_text": dtop1,
        "dtop1_text_se": dtop1_se,
    }
    if all("kl" in r for r in cand):
        kl, kl_se = mean_se(cat(cand, "kl"), clusters)
        out["kl_x1000"], out["kl_x1000_se"] = kl * 1000.0, kl_se * 1000.0
    return out


def within_noise(stats: Dict[str, Any], *, nll_floor: float, kl_max_x1000: float) -> tuple[bool, str]:
    """ΔNLL <= max(2·SE, floor) and KL×1000 <= bound. Returns (pass, one-line detail)."""
    se = stats["dnll_se"] if math.isfinite(stats["dnll_se"]) else 0.0
    bound = max(2.0 * se, nll_floor)
    ok_nll = stats["dnll"] <= bound
    kl = stats.get("kl_x1000")
    ok_kl = kl is None or kl <= kl_max_x1000
    detail = (
        f"ΔNLL {stats['dnll']:+.4f}±{se:.4f} nats/token (bound {bound:.4f}), "
        + (f"KL×1000 {kl:.1f} (max {kl_max_x1000:g}), " if kl is not None else "")
        + f"top-1 agree {stats['top1_agree']:.3f}, Δtop-1 {stats['dtop1_text']:+.3f} over {stats['positions']} positions"
    )
    return ok_nll and ok_kl, detail


def compare_decode(ref: List[Optional[torch.Tensor]], cand: List[Optional[torch.Tensor]], targets: List[torch.Tensor]) -> Optional[Dict[str, Any]]:
    """Traced-decode top-1 vs the text token, candidate minus reference."""
    rows = [(r, c, t) for r, c, t in zip(ref, cand, targets) if r is not None and c is not None]
    if not rows:
        return None
    hit_r = torch.cat([(r == t).double() for r, _, t in rows])
    hit_c = torch.cat([(c == t).double() for _, c, t in rows])
    clusters = clusters_for([int(t.numel()) for _, _, t in rows])
    d, se = mean_se(hit_c - hit_r, clusters)
    return {"positions": int(hit_r.numel()), "top1_ref": float(hit_r.mean()), "top1": float(hit_c.mean()),
            "dtop1": d, "dtop1_se": se,
            "agree": float(torch.cat([(r == c).double() for r, c, _ in rows]).mean())}


# --------------------------------------------------------------------------- device-facing (thin)


def score_generator(
    generator: Any,
    texts: Sequence[dict],
    *,
    topk: int,
    ref_scores: Optional[List[Dict[str, torch.Tensor]]] = None,
    decode_tokens: int = 0,
    decode_texts: int = 0,
) -> List[Dict[str, torch.Tensor]]:
    out = []
    for i, t in enumerate(texts):
        if i:
            generator.reset()
        ids = [int(x) for x in t["token_ids"]]
        logits = generator.prefill_logits(ids)
        ref = ref_scores[i] if ref_scores is not None else None
        row = score_logits(
            logits, ids, int(t["prompt_len"]), topk=topk,
            ref_topk_idx=ref["topk_idx"] if ref is not None else None,
            ref_topk_logp=ref["topk_logp"] if ref is not None else None,
        )
        del logits
        if decode_tokens and i < decode_texts and len(ids) - int(t["prompt_len"]) > decode_tokens:
            generator.reset()
            row["decode_pred"] = decode_teacher_forced(generator, ids, decode_tokens)
        out.append(row)
    return out


def decode_teacher_forced(generator: Any, ids: Sequence[int], n: int) -> torch.Tensor:
    """Traced decode over the last ``n`` tokens; returns the predictions for ids[-n:]."""
    prefix, forced = list(ids[:-n]), list(ids[-n:])
    preds: List[int] = []

    def next_input(step: int, predicted: int) -> int:
        preds.append(int(predicted))
        return int(forced[step])

    generator.generate(prefix, n, next_input=next_input, enable_trace=True)
    if len(preds) != n:
        raise RuntimeError(f"decode teacher forcing produced {len(preds)}/{n} predictions")
    return torch.tensor(preds, dtype=torch.int32)


def release(generator: Any) -> None:
    teardown = getattr(generator, "teardown", None)
    if callable(teardown):
        teardown()
    gc.collect()


def hf_weight_bytes(hf_model: str) -> Optional[int]:
    path = Path(hf_model)
    if not path.is_dir():
        try:
            from huggingface_hub import snapshot_download

            path = Path(snapshot_download(hf_model, local_files_only=True, allow_patterns=["*.safetensors", "*.bin", "*.json"]))
        except Exception:  # noqa: BLE001 - any lookup failure means "unknown size"
            return None
    files = list(path.glob("*.safetensors")) or list(path.glob("*.bin"))
    return sum(f.stat().st_size for f in files) or None


def mem_available_bytes() -> Optional[int]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def hf_fit_reason(hf_model: str) -> Optional[str]:
    """None when the HF model fits in host RAM, else why the HF slice is skipped."""
    need, have = hf_weight_bytes(hf_model), mem_available_bytes()
    if need is None:
        return (f"no local weight files for the HF checkpoint {hf_model} (its cached snapshot may hold only configs); "
                "pass the checkpoint directory as --hf-model, download it, or pass --hf-positions 0")
    if have is None:
        return "could not read MemAvailable"
    if need * 1.2 + HF_HEADROOM_BYTES > have:
        return f"HF weights {need / 2**30:.0f} GiB do not fit in {have / 2**30:.0f} GiB free host RAM"
    return None


def load_hf_model(hf_model: str) -> Any:
    from transformers import AutoModelForCausalLM

    return AutoModelForCausalLM.from_pretrained(hf_model, torch_dtype=torch.bfloat16).eval()


def score_hf(model: Any, texts: Sequence[dict], positions: int, ref_scores: List[Dict[str, torch.Tensor]]) -> List[Dict[str, torch.Tensor]]:
    out, scored = [], 0
    with torch.no_grad():
        for t, ref in zip(texts, ref_scores):
            if scored >= positions:
                break
            ids = [int(x) for x in t["token_ids"]]
            logits = model(input_ids=torch.tensor([ids])).logits
            out.append(score_logits(logits, ids, int(t["prompt_len"]), ref_topk_idx=ref["topk_idx"], ref_topk_logp=ref["topk_logp"]))
            scored += int(out[-1]["nll"].numel())
    return out


# --------------------------------------------------------------------------- orchestration


def load_candidates(path: Path, max_candidates: int) -> Dict[str, dict]:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and isinstance(data.get("candidates"), dict):
        data = data["candidates"]
    if not isinstance(data, dict) or not data or not all(isinstance(v, dict) for v in data.values()):
        raise SystemExit(f"{path}: expected a JSON object mapping candidate id -> build_generator kwargs")
    if len(data) > max_candidates:
        raise SystemExit(
            f"{path}: {len(data)} candidates exceed --max-candidates {max_candidates}; offer only the configs "
            "that fit with the full-context KV budget and that the sweep would select"
        )
    return {str(k): v for k, v in data.items()}


def scores_path(model_dir: Path, cid: str) -> Path:
    return model_dir / SCORES_DIR / f"{cid}.pt"


def load_scores(model_dir: Path, cid: str, sha: str, kwargs: dict, against: Optional[dict]) -> Optional[list]:
    """Cached rows for ``cid`` when the text, its build kwargs and its role match.

    ``against`` is None for the reference (rows hold its top-k) and
    ``{"id", "kwargs"}`` of the reference for a candidate (rows hold KL against
    it), so a run with a different ``--reference`` rescores instead of reusing
    rows of the wrong kind.
    """
    path = scores_path(model_dir, cid)
    if not path.is_file():
        return None
    blob = torch.load(path, weights_only=False)
    if blob.get("text_sha") != sha or blob.get("build_kwargs") != kwargs or blob.get("scored_against", "unknown") != against:
        return None
    return blob["texts"]


def save_scores(model_dir: Path, cid: str, sha: str, kwargs: dict, against: Optional[dict], rows: list) -> None:
    path = scores_path(model_dir, cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"text_sha": sha, "build_kwargs": kwargs, "scored_against": against, "texts": rows}, path)


def _fmt(cid: str, s: Dict[str, Any], ok: bool) -> str:
    kl = f" KLx1000={s['kl_x1000']:.1f}" if "kl_x1000" in s else ""
    return (f"{cid}: dNLL={s['dnll']:+.4f}+-{s['dnll_se']:.4f}{kl} top1agree={s['top1_agree']:.3f} "
            f"dtop1={s['dtop1_text']:+.3f} -> {'within noise' if ok else 'WORSE than noise'}")


def run(
    args: argparse.Namespace,
    *,
    build_generator: Callable[..., Any],
    mesh_device: Any = None,
    tokenizer: Any = None,
    hf_loader: Callable[[str], Any] = load_hf_model,
    hf_fit: Callable[[str], Optional[str]] = hf_fit_reason,
    log: Callable[[str], None] = print,
) -> int:
    model_dir = Path(args.model_dir)
    candidates = load_candidates(args.candidates, args.max_candidates)
    ids = list(candidates)
    ref_id = args.reference or ids[0]
    sel_id = args.selected or ref_id
    for name, cid in (("--reference", ref_id), ("--selected", sel_id)):
        if cid not in candidates:
            raise SystemExit(f"{name} {cid!r} is not in {args.candidates} ({', '.join(ids)})")
    template_kwargs = json.loads(args.chat_template_kwargs) if args.chat_template_kwargs else dict(DEFAULT_CHAT_TEMPLATE_KWARGS)
    order = [ref_id] + [c for c in ids if c != ref_id]
    if args.only:
        if args.only not in candidates:
            raise SystemExit(f"--only {args.only!r} is not a candidate")
        order = [ref_id] if args.only == ref_id else [ref_id, args.only]

    text_path = model_dir / TEXT_CACHE
    texts: List[dict] = []
    if args.text_file:
        texts = texts_from_file(args.text_file, tokenizer, template_kwargs, args.max_text_tokens)[: args.num_texts]
        write_text_cache(text_path, texts)
    elif text_path.is_file():
        texts = read_text_cache(text_path)
    scores: Dict[str, list] = {}
    sha = text_sha(texts) if texts else ""
    errors: Dict[str, str] = {}

    for cid in order:
        kwargs = candidates[cid]
        against = None if cid == ref_id else {"id": ref_id, "kwargs": candidates[ref_id]}
        cached = load_scores(model_dir, cid, sha, kwargs, against) if texts else None
        if cached is not None:
            scores[cid] = cached
            continue
        if cid != ref_id and ref_id not in scores:
            errors[cid] = "reference not scored"
            continue
        log(f"building candidate {cid}")
        try:
            gen = build_generator(model_dir=model_dir, mesh_device=mesh_device, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a candidate that cannot build is reported, not fatal
            errors[cid] = f"build_generator failed: {type(exc).__name__}: {exc}"
            log(errors[cid])
            continue
        try:
            if not texts:
                if tokenizer is None:
                    raise SystemExit("generating the text set needs --hf-model for the chat template")
                texts = generate_texts(
                    gen, tokenizer, load_prompts(args.prompts_file), num_texts=args.num_texts,
                    max_new_tokens=args.max_new_tokens, sampling=args.sampling, template_kwargs=template_kwargs,
                    min_answer_tokens=args.min_answer_tokens, max_text_tokens=args.max_text_tokens, log=log,
                )
                for t in texts:
                    t["config"] = ref_id
                write_text_cache(text_path, texts)
                sha = text_sha(texts)
                gen.reset()
            rows = score_generator(
                gen, texts, topk=args.topk, ref_scores=scores.get(ref_id) if cid != ref_id else None,
                decode_tokens=args.decode_tokens, decode_texts=args.decode_texts,
            )
            save_scores(model_dir, cid, sha, kwargs, against, rows)
            scores[cid] = rows
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            errors[cid] = f"scoring failed: {type(exc).__name__}: {exc}"
            log(errors[cid])
        finally:
            release(gen)
            del gen
            gc.collect()

    return finish(args, model_dir, texts, sha, candidates, ref_id, sel_id, scores, errors, hf_loader, hf_fit, log)


def finish(args, model_dir, texts, sha, candidates, ref_id, sel_id, scores, errors, hf_loader, hf_fit, log) -> int:
    from readiness_check.eval_readiness_result import check, print_summary, write_result

    ref = scores.get(ref_id)
    per: Dict[str, Any] = {}
    lines = [f"texts: {len(texts)} ({texts[0].get('source', '?') if texts else 'none'}), sha {sha}, reference {ref_id}"]
    for cid in candidates:
        if cid == ref_id or ref is None:
            continue
        if cid not in scores:
            per[cid] = {"error": errors.get(cid, "not scored in this run (see --only)")}
            continue
        stats = compare(ref, scores[cid])
        ok, detail = within_noise(stats, nll_floor=args.nll_floor, kl_max_x1000=args.kl_max_x1000)
        stats["within_noise"], stats["detail"] = ok, detail
        targets = [r["target"][-args.decode_tokens:] if args.decode_tokens else r["target"] for r in ref]
        dec = compare_decode([r.get("decode_pred") for r in ref], [c.get("decode_pred") for c in scores[cid]], targets)
        if dec:
            stats["decode"] = dec
        per[cid] = stats
        lines.append(_fmt(cid, stats, ok))

    checks = []
    positions = sum(int(r["nll"].numel()) for r in ref) if ref else 0
    checks.append(check(
        "positions_scored", positions >= args.min_positions, waivable=False, value=positions, threshold=args.min_positions,
        detail=(f"{positions} answer positions scored" if ref else f"reference {ref_id} not scored: {errors.get(ref_id, 'no texts')}")
        + ("" if positions >= args.min_positions else "; generate more or longer texts (--num-texts, --max-new-tokens)"),
    ))
    if sel_id == ref_id:
        checks.append(check("selected_within_noise", True if ref else False, value=0.0,
                            detail=f"selected config is the reference {ref_id}"))
    elif "dnll" in per.get(sel_id, {}):
        s = per[sel_id]
        checks.append(check(
            "selected_within_noise", s["within_noise"], value=round(s["dnll"], 5),
            threshold=round(max(2 * (s["dnll_se"] if math.isfinite(s["dnll_se"]) else 0.0), args.nll_floor), 5),
            detail=f"{sel_id} vs {ref_id}: {s['detail']}"
            + ("" if s["within_noise"] else "; restore the most sensitive BFP4 group to BFP8, or record the loss as a limitation"),
        ))
    else:
        checks.append(check("selected_within_noise", False,
                            detail=f"{sel_id} not scored: {per.get(sel_id, {}).get('error', errors.get(sel_id, 'unknown'))}"))

    dec = per.get(sel_id, {}).get("decode")
    if sel_id == ref_id or not args.decode_tokens:
        checks.append(check("decode_top1_within_noise", None, severity="advisory",
                            detail="decode teacher forcing off (--decode-tokens 0)" if not args.decode_tokens else "selected is the reference"))
    elif dec is None:
        checks.append(check("decode_top1_within_noise", None, severity="advisory",
                            detail="no decode predictions (texts shorter than --decode-tokens or scoring failed)"))
    else:
        bound = -max(2 * (dec["dtop1_se"] if math.isfinite(dec["dtop1_se"]) else 0.0), DECODE_TOP1_FLOOR)
        checks.append(check(
            "decode_top1_within_noise", dec["dtop1"] >= bound, severity="advisory", value=round(dec["dtop1"], 4),
            threshold=round(bound, 4),
            detail=f"traced decode top-1 {dec['top1']:.3f} vs reference {dec['top1_ref']:.3f} over {dec['positions']} positions",
        ))
        lines.append(f"decode {sel_id}: top1 {dec['top1']:.3f} vs ref {dec['top1_ref']:.3f}, agree {dec['agree']:.3f}")

    hf_stats = None
    if not args.hf_positions:
        checks.append(check("reference_vs_hf", None, severity="advisory", detail="HF slice off (--hf-positions 0)"))
    elif ref is None or not args.hf_model:
        checks.append(check("reference_vs_hf", None, severity="advisory",
                            detail="no --hf-model" if not args.hf_model else "reference not scored"))
    else:
        reason = hf_fit(args.hf_model)
        if reason:
            checks.append(check("reference_vs_hf", None, severity="advisory", detail=reason))
        else:
            hf_rows = score_hf(hf_loader(args.hf_model), texts, args.hf_positions, ref)
            # KL(ref || HF) on the reference's top-k; ΔNLL = HF - reference.
            hf_stats = compare(ref[: len(hf_rows)], hf_rows)
            ok = hf_stats.get("kl_x1000", 0.0) <= args.hf_kl_max_x1000
            checks.append(check(
                "reference_vs_hf", ok, severity="advisory", value=round(hf_stats.get("kl_x1000", 0.0), 2),
                threshold=args.hf_kl_max_x1000,
                detail=f"KL(reference||HF)×1000 {hf_stats.get('kl_x1000', 0.0):.1f}, top-1 agree {hf_stats['top1_agree']:.3f}, "
                f"NLL HF-ref {hf_stats['dnll']:+.4f} over {hf_stats['positions']} positions"
                + ("" if ok else "; the reference itself loses accuracy (approximate activations, fidelity, BFP weights)"),
            ))
            lines.append(f"HF vs {ref_id}: KLx1000={hf_stats.get('kl_x1000', 0.0):.1f} top1agree={hf_stats['top1_agree']:.3f}")

    data = {
        "reference": ref_id,
        "selected": sel_id,
        "text": {"count": len(texts), "sha": sha, "positions": positions, "cache": str(TEXT_CACHE),
                 "answer_tokens": [len(t["token_ids"]) - int(t["prompt_len"]) for t in texts]},
        "thresholds": {"nll_floor": args.nll_floor, "kl_max_x1000": args.kl_max_x1000,
                       "hf_kl_max_x1000": args.hf_kl_max_x1000, "min_positions": args.min_positions,
                       "rule": "dNLL <= max(2*SE, nll_floor) and KLx1000 <= kl_max_x1000"},
        "candidates": {k: _compact(v) for k, v in per.items()},
        "errors": errors,
    }
    if hf_stats:
        data["hf"] = _compact(hf_stats)
    path = write_result(model_dir, PROBE, checks, summary_lines=lines, data=data)
    return print_summary(path)


def _compact(d: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in d.items():
        if isinstance(v, float):
            out[k] = round(v, 5) if math.isfinite(v) else None
        elif isinstance(v, dict):
            out[k] = _compact(v)
        else:
            out[k] = v
    return out


def model_card_sampling(hf_model: Optional[str]) -> dict:
    """temperature/top_p/top_k from the model's generation_config.json; a neutral default otherwise."""
    if hf_model:
        try:
            from transformers import GenerationConfig

            g = GenerationConfig.from_pretrained(hf_model)
            out = {k: getattr(g, k) for k in ("temperature", "top_p", "top_k") if getattr(g, k, None) is not None}
            if out:
                return out
        except Exception:  # noqa: BLE001
            pass
    return {"temperature": 1.0, "top_p": 0.95}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="P1: precision candidates teacher-forced on long reasoning text.")
    p.add_argument("--model-dir", type=Path, required=True)
    p.add_argument("--hf-model", default="", help="HF id or path: chat template, sampling defaults and the HF slice")
    p.add_argument("--candidates", type=Path, required=True, help="JSON: candidate id -> build_generator kwargs")
    p.add_argument("--reference", default="", help="highest-precision candidate that fits (default: first)")
    p.add_argument("--selected", default="", help="the candidate the sweep selected (default: the reference)")
    p.add_argument("--only", default="", help="score only this candidate (plus the reference if missing); others come from the cache")
    p.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    p.add_argument("--num-texts", type=int, default=DEFAULT_NUM_TEXTS)
    p.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS)
    p.add_argument("--max-text-tokens", type=int, default=DEFAULT_MAX_TEXT_TOKENS)
    p.add_argument("--min-answer-tokens", type=int, default=DEFAULT_MIN_ANSWER_TOKENS)
    p.add_argument("--text-file", type=Path, default=None, help="JSONL traces instead of generated text (replaces the cache)")
    p.add_argument("--prompts-file", type=Path, default=DEFAULT_PROMPTS)
    p.add_argument("--sampling", default="", help='JSON, e.g. {"temperature":1.0,"top_p":0.95}; default generation_config.json')
    p.add_argument("--chat-template-kwargs", default="", help=f"JSON; default {json.dumps(DEFAULT_CHAT_TEMPLATE_KWARGS)}")
    p.add_argument("--topk", type=int, default=DEFAULT_TOPK)
    p.add_argument("--decode-tokens", type=int, default=DEFAULT_DECODE_TOKENS)
    p.add_argument("--decode-texts", type=int, default=DEFAULT_DECODE_TEXTS)
    p.add_argument("--hf-positions", type=int, default=DEFAULT_HF_POSITIONS)
    p.add_argument("--min-positions", type=int, default=DEFAULT_MIN_POSITIONS)
    p.add_argument("--nll-floor", type=float, default=DEFAULT_NLL_FLOOR)
    p.add_argument("--kl-max-x1000", type=float, default=DEFAULT_KL_MAX_X1000)
    p.add_argument("--hf-kl-max-x1000", type=float, default=DEFAULT_HF_KL_MAX_X1000)
    return p


def finalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.sampling = json.loads(args.sampling) if args.sampling else model_card_sampling(args.hf_model or None)
    return args


def main(argv: Optional[List[str]] = None) -> int:
    from readiness_check.build_kwargs import add_build_kwargs_args, parse_build_kwargs
    from readiness_check.mesh_device import add_mesh_device_args, close_readiness_mesh_device, open_readiness_mesh_device
    from readiness_check.run_teacher_forcing import _import_build_generator

    parser = build_parser()
    add_mesh_device_args(parser)
    add_build_kwargs_args(parser)
    args = finalize_args(parser.parse_args(argv))
    common = parse_build_kwargs(args)  # applied under every candidate's own kwargs
    model_dir = args.model_dir.resolve()
    args.model_dir = model_dir
    build = _import_build_generator(model_dir)

    def build_with_common(**kw):
        merged = dict(common)
        merged.update({k: v for k, v in kw.items() if k not in ("model_dir", "mesh_device")})
        return build(model_dir=kw["model_dir"], mesh_device=kw["mesh_device"], **merged)

    tokenizer = None
    if args.hf_model:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(args.hf_model)
    mesh = open_readiness_mesh_device(args.mesh_device, args.fabric_config)
    try:
        return run(args, build_generator=build_with_common, mesh_device=mesh, tokenizer=tokenizer)
    finally:
        close_readiness_mesh_device(mesh, args.fabric_config)


if __name__ == "__main__":
    sys.exit(main())
