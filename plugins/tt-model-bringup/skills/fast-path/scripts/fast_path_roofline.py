#!/usr/bin/env python3
"""Batch-1 decode roofline from a Hugging Face config: bytes per token, DRAM floor, % of peak.

Decode at batch 1 reads every active weight once per token, so the step time is bounded below by
bytes per chip divided by DRAM bandwidth. This counts the weights one token reads, per role, at the
chosen formats, for each tensor-parallel width, and compares with measured steps when given.

Dense and MoE decoders are supported (Llama/Qwen/Mistral/gpt-oss/Mixtral-style configs, with
DeepSeek- and Qwen-MoE-style dense layers and shared experts). Anything else, such as latent
attention, needs --override for the per-layer parameter counts it cannot derive.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BYTES_PER_VALUE = {"bf16": 2.0, "fp16": 2.0, "fp32": 4.0, "bfp8": 1.0625, "bfp4": 0.5625, "mxfp4": 0.53125, "int8": 1.0}
DEFAULT_FORMATS = {"attention": "bfp8", "dense_mlp": "bfp8", "experts": "bfp8", "shared_experts": "bfp8",
                   "router": "bf16", "lm_head": "bfp8", "kv_cache": "bfp8"}
OVERRIDABLE = ("attention_params_per_layer", "dense_mlp_params_per_layer", "expert_params", "shared_expert_params",
               "router_params_per_layer", "lm_head_params", "kv_bytes_per_token_per_layer")


def first(cfg: dict, *keys, default=None):
    for k in keys:
        if cfg.get(k) is not None:
            return cfg[k]
    return default


def text_config(cfg: dict) -> dict:
    """Multimodal configs nest the decoder under text_config / language_config."""
    for k in ("text_config", "language_config", "llm_config"):
        if isinstance(cfg.get(k), dict) and cfg[k].get("hidden_size"):
            return cfg[k]
    return cfg


def layer_kinds(cfg: dict, layers: int) -> list[str]:
    """'moe' or 'dense' per layer, from the config's sparse-layer conventions."""
    experts = first(cfg, "num_local_experts", "num_experts", "n_routed_experts", default=0) or 0
    if not experts:
        return ["dense"] * layers
    kinds = ["moe"] * layers
    for i in range(min(int(first(cfg, "first_k_dense_replace", default=0) or 0), layers)):
        kinds[i] = "dense"
    for i in first(cfg, "mlp_only_layers", default=[]) or []:
        if 0 <= i < layers:
            kinds[i] = "dense"
    step = int(first(cfg, "decoder_sparse_step", "moe_layer_freq", default=1) or 1)
    if step > 1:
        for i in range(layers):
            if (i + 1) % step != 0:
                kinds[i] = "dense"
    return kinds


def attention_kinds(cfg: dict, layers: int) -> list[str]:
    types = cfg.get("layer_types")
    if isinstance(types, list) and len(types) == layers:
        return ["sliding" if "sliding" in str(t) else "full" for t in types]
    return ["full"] * layers


def count(cfg: dict, formats: dict, overrides: dict, context: int, vocab_pad: int, mlp_matrices: int) -> dict:
    cfg = text_config(cfg)
    hidden = int(first(cfg, "hidden_size", "d_model", "n_embd"))
    layers = int(first(cfg, "num_hidden_layers", "n_layer", "num_layers"))
    heads = int(first(cfg, "num_attention_heads", "n_head"))
    kv_heads = int(first(cfg, "num_key_value_heads", "num_kv_heads", default=heads))
    head_dim = int(first(cfg, "head_dim", default=hidden // heads))
    vocab = int(first(cfg, "vocab_size"))
    if vocab_pad > 1:
        vocab = -(-vocab // vocab_pad) * vocab_pad
    experts = int(first(cfg, "num_local_experts", "num_experts", "n_routed_experts", default=0) or 0)
    top_k = int(first(cfg, "num_experts_per_tok", "moe_topk", "num_selected_experts", default=0) or 0)
    dense_inter = int(first(cfg, "intermediate_size", "ffn_dim", default=4 * hidden))
    expert_inter = int(first(cfg, "moe_intermediate_size", "expert_intermediate_size", default=dense_inter))
    shared = int(first(cfg, "n_shared_experts", "num_shared_experts", default=0) or 0)
    shared_inter = int(first(cfg, "shared_expert_intermediate_size", default=shared * expert_inter) or 0)
    if cfg.get("kv_lora_rank") and "attention_params_per_layer" not in overrides:
        raise SystemExit("latent attention (kv_lora_rank) needs --override attention_params_per_layer=<n> "
                         "and kv_bytes_per_token_per_layer=<n>")

    q_dim, kv_dim = heads * head_dim, kv_heads * head_dim
    per = {
        "attention_params_per_layer": hidden * q_dim + 2 * hidden * kv_dim + q_dim * hidden,
        "dense_mlp_params_per_layer": mlp_matrices * hidden * dense_inter,
        "expert_params": mlp_matrices * hidden * expert_inter,
        "shared_expert_params": mlp_matrices * hidden * shared_inter,
        "router_params_per_layer": hidden * experts,
        "lm_head_params": hidden * vocab,
        "kv_bytes_per_token_per_layer": 2 * kv_dim * BYTES_PER_VALUE[formats["kv_cache"]],
    }
    per.update(overrides)

    kinds, attn = layer_kinds(cfg, layers), attention_kinds(cfg, layers)
    window = int(cfg.get("sliding_window") or 0)
    b = {k: BYTES_PER_VALUE[v] for k, v in formats.items()}
    roles = {"attention": layers * per["attention_params_per_layer"] * b["attention"],
             "lm_head": per["lm_head_params"] * b["lm_head"]}
    n_dense, n_moe = kinds.count("dense"), kinds.count("moe")
    if n_dense:
        roles["dense_mlp"] = n_dense * per["dense_mlp_params_per_layer"] * b["dense_mlp"]
    if n_moe:
        roles["experts"] = n_moe * top_k * per["expert_params"] * b["experts"]
        roles["router"] = n_moe * per["router_params_per_layer"] * b["router"]
        if per["shared_expert_params"]:
            roles["shared_experts"] = n_moe * per["shared_expert_params"] * b["shared_experts"]
    kv_tokens = [min(context, window) if (a == "sliding" and window) else context for a in attn]
    roles["kv_cache"] = sum(t * per["kv_bytes_per_token_per_layer"] for t in kv_tokens)
    return {"model": {"hidden": hidden, "layers": layers, "heads": heads, "kv_heads": kv_heads, "head_dim": head_dim,
                      "vocab_rows": vocab, "experts": experts, "experts_per_token": top_k, "dense_layers": n_dense,
                      "moe_layers": n_moe},
            "formats": formats, "context": context, "gb_per_token_by_role": {k: v / 1e9 for k, v in roles.items()},
            "gb_per_token": sum(roles.values()) / 1e9}


def parse_pairs(items, cast=str, what="key=value") -> dict:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"expected {what}, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = cast(v.strip())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="HF config.json (a file, or a directory containing one)")
    ap.add_argument("--tp", type=int, nargs="+", default=[1, 2, 4], help="tensor-parallel widths (chips) to report")
    ap.add_argument("--format", nargs="*", default=[], metavar="ROLE=FMT",
                    help=f"roles: {', '.join(DEFAULT_FORMATS)}; formats: {', '.join(BYTES_PER_VALUE)}")
    ap.add_argument("--context", type=int, default=128, help="tokens of context the KV-cache read covers")
    ap.add_argument("--vocab-pad", type=int, default=1, help="pad the LM-head rows to a multiple of this")
    ap.add_argument("--mlp-matrices", type=int, default=3, choices=(2, 3), help="3 for gated MLPs, 2 otherwise")
    ap.add_argument("--replicated", nargs="*", default=[], metavar="ROLE", help="roles every chip reads in full")
    ap.add_argument("--override", nargs="*", default=[], metavar="NAME=N", help=f"one of: {', '.join(OVERRIDABLE)}")
    ap.add_argument("--peak-gbps", type=float, default=512.0, help="DRAM peak per chip (Blackhole P150: about 512)")
    ap.add_argument("--measured-ms", nargs="*", default=[], metavar="TP=MS", help="measured batch-1 step per width")
    ap.add_argument("--json", help="write the result here")
    a = ap.parse_args(argv)

    path = Path(a.config)
    path = path / "config.json" if path.is_dir() else path
    cfg = json.loads(path.read_text())
    formats = dict(DEFAULT_FORMATS, **parse_pairs(a.format, str, "ROLE=FMT"))
    bad = {k: v for k, v in formats.items() if k not in DEFAULT_FORMATS or v not in BYTES_PER_VALUE}
    if bad:
        raise SystemExit(f"unknown role or format: {bad}")
    overrides = parse_pairs(a.override, float, "NAME=N")
    unknown = set(overrides) - set(OVERRIDABLE)
    if unknown:
        raise SystemExit(f"unknown override: {sorted(unknown)}")
    measured = {int(k): v for k, v in parse_pairs(a.measured_ms, float, "TP=MS").items()}

    res = count(cfg, formats, overrides, a.context, a.vocab_pad, a.mlp_matrices)
    roles = res["gb_per_token_by_role"]
    res["peak_gbps_per_chip"] = a.peak_gbps
    res["meshes"] = {}
    for tp in a.tp:
        per_chip = sum(v if k in a.replicated else v / tp for k, v in roles.items())
        row = {"tp": tp, "gb_per_chip": per_chip, "floor_ms": per_chip / a.peak_gbps * 1e3}
        if tp in measured:
            ms = measured[tp]
            row.update(measured_ms=ms, achieved_gbps_per_chip=per_chip / ms * 1e3,
                       pct_of_peak=100.0 * row["floor_ms"] / ms)
        res["meshes"][str(tp)] = row

    print(f"{path}: {res['model']['layers']} layers, hidden {res['model']['hidden']}, "
          f"{res['model']['moe_layers']} MoE / {res['model']['dense_layers']} dense, context {a.context}")
    for k, v in sorted(roles.items(), key=lambda kv: -kv[1]):
        print(f"  {k:15s} {v:8.3f} GB/token  ({formats[k]})")
    print(f"  {'total':15s} {res['gb_per_token']:8.3f} GB/token")
    for tp, row in res["meshes"].items():
        line = f"  TP={tp}: {row['gb_per_chip']:.3f} GB/chip, floor {row['floor_ms']:.2f} ms at {a.peak_gbps:g} GB/s"
        if "measured_ms" in row:
            line += (f"; measured {row['measured_ms']:.2f} ms = {row['achieved_gbps_per_chip']:.0f} GB/s per chip, "
                     f"{row['pct_of_peak']:.0f}% of peak")
            if row["pct_of_peak"] > 100:
                line += "  <-- above peak: a role is missing, replicated or mis-sized"
        print(line)
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
