# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""P5: long context on the served path (recall at stage 9, timing and chunk size at stage 10).

Attach to a running vLLM server on the chip path (``sample_on_device_mode`` as served).

``--part accuracy`` (stage 9, ``long_context_accuracy``): a needle fact inside long
natural text (the readiness book) at a few depths, at about 8K, 32K and 128K tokens
(clamped to the served ``max_model_len``), asked through the chat template at
temperature 0. Each length passes when every answer contains the needle. A failure is
critical and waivable only by the HF control rule (limitations.json with
``original_model: "same"``) or the fix time box.

``--part timing`` (stage 10, ``long_context_timing``): single-user decode ms/token at
each length (short output, ``ignore_eos``), TTFT for 127- vs 128-token prompts
(ratio > 1.5 is advisory), and a cliff check (TPOT more than 2x between adjacent
lengths, advisory). It also folds in the chunk-size sweep files written by
``--part chunk``.

``--part chunk`` (stage 10, once per ``max_num_batched_tokens`` value, server
restarted for each): TTFT of a 4K and a 32K prompt while ``--decode-users`` streams
decode, and the worst inter-token gap those streams saw during each prefill. Writes
``doc/eval_readiness/long_context_timing/chunk_<mnbt>.json``. The timing part picks
the largest value whose worst gap meets ``--itl-bound-ms``.

    python -m readiness_check.run_long_context --part accuracy --server-url http://localhost:8000 \\
        --model-dir models/autoports/<model> --server-log <server.log>

Exit 0 pass, 1 advisory, 2 critical, 3 error.
"""

from __future__ import annotations

import argparse
import bz2
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from readiness_check import served_probe as sp
from readiness_check.eval_readiness_result import RESULT_DIR, check, print_summary, write_result

ACCURACY_PROBE = "long_context_accuracy"
TIMING_PROBE = "long_context_timing"
BOOK_RELPATHS = (
    Path("models/ttt_compat/tests/tale-of-two-cities.txt.bz2"),
    Path("models/tt_transformers/tests/tale-of-two-cities.txt.bz2"),
)
DEFAULT_ACCURACY_LENGTHS = "8192,32768,131072"
DEFAULT_TIMING_LENGTHS = "1024,8192,32768,131072"
CLIFF_RATIO = 2.0
TTFT_ALIGN_RATIO = 1.5
DEFAULT_ITL_BOUND_MS = 1000.0
DEFAULT_CHUNK_VALUES = (512, 2048, 4096)
WORDS = ("amber", "cobalt", "falcon", "harbor", "juniper", "lantern", "meadow", "orchid", "quartz", "saffron")


# ---------------------------------------------------------------- text


def find_book(text_file: Optional[Path], tt_metal: Optional[Path]) -> Path:
    if text_file:
        if not text_file.is_file():
            raise sp.ProbeError(f"--text-file {text_file} does not exist")
        return text_file
    roots = [p for p in (tt_metal, os.environ.get("TT_METAL_HOME"), Path.cwd()) if p]
    roots += list(Path.cwd().parents)
    for root in roots:
        for rel in BOOK_RELPATHS:
            candidate = Path(root) / rel
            if candidate.is_file():
                return candidate
    raise sp.ProbeError("readiness book not found; pass --text-file or --tt-metal "
                        f"(looked for {', '.join(map(str, BOOK_RELPATHS))})")


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix == ".bz2":
        raw = bz2.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    return " ".join(text.split())


def long_text(book: str, chars: int) -> str:
    out = book
    while len(out) < chars:
        out += " " + book
    return out[:chars]


def needle_for(seed: int) -> tuple[str, str, str]:
    rng = random.Random(seed)
    answer = f"{rng.choice(WORDS)}-{rng.randint(1000, 9999)}"
    fact = f" The secret passphrase for the lighthouse keeper is {answer}. "
    question = ("What is the secret passphrase for the lighthouse keeper mentioned in the text above? "
                "Answer with the passphrase only.")
    return answer, fact, question


def insert_at(text: str, fact: str, depth: float) -> str:
    pos = int(len(text) * depth)
    stop = text.find(". ", pos)
    pos = stop + 1 if 0 <= stop < pos + 2000 else pos
    return text[:pos] + fact + text[pos:]


def recall_prompt(book: str, target_tokens: int, depth: float, seed: int, count_tokens) -> tuple[str, str, int]:
    """Prompt of about ``target_tokens`` chat tokens with the needle at ``depth``; returns (prompt, answer, tokens)."""
    answer, fact, question = needle_for(seed)
    chars_per_token = 4.0
    prompt, tokens = "", 0
    for _ in range(4):
        body_chars = max(200, int((target_tokens - 64) * chars_per_token))
        prompt = insert_at(long_text(book, body_chars), fact, depth) + "\n\n" + question
        tokens = count_tokens(prompt)
        if tokens <= 0:
            break
        if abs(tokens - target_tokens) <= max(64, 0.02 * target_tokens):
            break
        chars_per_token = len(prompt) / tokens
    return prompt, answer, tokens


def contains_answer(answer: str, *texts: str) -> bool:
    want = answer.lower().replace("-", "")
    return any(want in (t or "").lower().replace("-", "").replace(" ", "") for t in texts)


def clamp_lengths(lengths: list[int], max_model_len: Optional[int], reserve: int) -> list[int]:
    if not max_model_len:
        return lengths
    top = max_model_len - reserve
    return sorted({min(length, top) for length in lengths if top > 0})


# ---------------------------------------------------------------- parts


def run_accuracy(args, server: str, model: str, max_len: Optional[int], log: sp.ServerLog) -> Path:
    profile = sp.load_profile(args.profile, args.profiles_file)
    book = read_text(find_book(args.text_file, args.tt_metal))
    chat_kwargs = {"enable_thinking": bool(args.thinking)}
    lengths = clamp_lengths(sp.parse_csv_ints(args.lengths or DEFAULT_ACCURACY_LENGTHS), max_len, args.max_tokens + 64)
    depths = sp.parse_csv_floats(args.depths)

    def count(prompt: str) -> int:
        return len(sp.tokenize(server, model, messages=[{"role": "user", "content": prompt}], chat_kwargs=chat_kwargs))

    rows, checks = [], []
    chat_profile = {"chat": True, "thinking": bool(args.thinking), "request": {}}
    for length in lengths:
        results = []
        for i, depth in enumerate(depths):
            seed = length * 31 + i
            prompt, answer, tokens = recall_prompt(book, length, depth, seed, count)
            sp.reset_prefix_cache(server)
            url, body = sp.build_request(server, model, chat_profile, content=prompt, max_tokens=args.max_tokens,
                                         overrides={"temperature": 0.0})
            rec = sp.stream_request(url, body, timeout=args.request_timeout)
            ok = not rec.get("error") and contains_answer(answer, rec.get("text"), rec.get("reasoning"))
            results.append({"length": length, "prompt_tokens": tokens, "depth": depth, "answer": answer, "pass": ok,
                            "error": rec.get("error"), "finish_reason": rec.get("finish_reason"),
                            "output_tail": ((rec.get("text") or "") or (rec.get("reasoning") or ""))[-200:],
                            "ttft_ms": sp.r(sp.ttft_ms(rec), 1)})
        rows += results
        failed = [r for r in results if not r["pass"]]
        detail = (f"{len(failed)}/{len(results)} depths missed the needle at ~{length} tokens "
                  f"(e.g. depth {failed[0]['depth']}: {failed[0]['error'] or repr(failed[0]['output_tail'][-80:])}); "
                  "run the same prompt on the HF model and on the host-sampling path before fixing"
                  if failed else f"needle recalled at all {len(results)} depths")
        checks.append(check(f"recall_{length}", not failed, value=len(results) - len(failed), threshold=len(results),
                            detail=detail))
    mode = sp.sample_on_device_mode(log.text(0)) if log.available else None
    on_device = mode is not None and mode.lower() not in ("none", "off", "false", "host")
    checks.append(check(
        "served_on_device_sampling", on_device, severity="advisory", value=mode,
        detail=(f"server log sample_on_device_mode={mode}" if on_device else
                f"sample_on_device_mode={mode or 'unknown'}; the recall must run on the chip sampling path "
                "(pass --server-log, serve with sample_on_device_mode=all)")))
    if max_len and max(sp.parse_csv_ints(args.lengths or DEFAULT_ACCURACY_LENGTHS)) > max(lengths, default=0):
        checks.append(check("context_coverage", None, detail=f"lengths clamped to max_model_len {max_len}"))
    raw = Path(args.model_dir) / RESULT_DIR / ACCURACY_PROBE
    sp.write_jsonl(raw / "cases.jsonl", rows)
    lines = [f"lengths {lengths} depths {depths} max_model_len {max_len} thinking {bool(args.thinking)}",
             f"sample_on_device_mode {mode}; temperature 0",
             *[f"~{r['length']} tok ({r['prompt_tokens']}) depth {r['depth']}: {'ok' if r['pass'] else 'MISS'}" for r in rows[:9]]]
    return write_result(args.model_dir, ACCURACY_PROBE, checks, summary_lines=lines,
                        data={"model": model, "max_model_len": max_len, "sample_on_device_mode": mode,
                              "cases": [{k: r[k] for k in ("length", "prompt_tokens", "depth", "pass")} for r in rows]},
                        output=args.output)


def book_ids(args, server: str, model: str, need: int) -> list[int]:
    book = read_text(find_book(args.text_file, args.tt_metal))
    ids = sp.tokenize(server, model, prompt=book)
    if not ids:
        raise sp.ProbeError("/tokenize returned no tokens for the book")
    out = list(ids)
    while len(out) < need:
        out += ids
    return out


def completion(server: str, model: str, ids: list[int], max_tokens: int, timeout: float, stop=None) -> dict:
    body = {"model": model, "prompt": ids, "max_tokens": max_tokens, "temperature": 0.0, "ignore_eos": True}
    return sp.stream_request(f"{server}/v1/completions", body, timeout=timeout, stop=stop, keep_text=False)


def chunk_sweep_rows(raw_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(raw_dir.glob("chunk_*.json")):
        try:
            rows.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(rows, key=lambda row: row.get("max_num_batched_tokens") or 0)


def pick_chunk(rows: list[dict], bound_ms: float) -> Optional[int]:
    ok = [row["max_num_batched_tokens"] for row in rows
          if isinstance(row.get("worst_itl_ms"), (int, float)) and row["worst_itl_ms"] <= bound_ms and not row.get("errors")]
    return max(ok) if ok else None


def run_timing(args, server: str, model: str, max_len: Optional[int], log: sp.ServerLog) -> Path:
    lengths = clamp_lengths(sp.parse_csv_ints(args.lengths or DEFAULT_TIMING_LENGTHS), max_len, args.decode_tokens + 16)
    ids = book_ids(args, server, model, max(lengths + [200 * (args.ttft_reps + 1) + 128]))
    checks, rows, errors = [], [], []
    for length in lengths:
        sp.reset_prefix_cache(server)
        rec = completion(server, model, ids[:length], args.decode_tokens, args.request_timeout)
        rows.append({"length": length, "tpot_ms": sp.r(sp.tpot_ms(rec)), "ttft_ms": sp.r(sp.ttft_ms(rec), 1),
                     "error": rec.get("error")})
        if rec.get("error"):
            errors.append(f"{length}: {rec['error']}")
    ttft: dict[int, list[Optional[float]]] = {127: [], 128: []}
    for rep in range(args.ttft_reps + 1):  # first round warms both shapes
        for n in (127, 128):
            offset = 200 * rep + (0 if n == 127 else 100)  # distinct tokens: no prefix-cache hit
            sp.reset_prefix_cache(server)
            rec = completion(server, model, ids[offset:offset + n], 4, args.request_timeout)
            if rec.get("error"):
                errors.append(f"ttft {n}: {rec['error']}")
            elif rep:
                ttft[n].append(sp.ttft_ms(rec))
    t127, t128 = sp.median(ttft[127]), sp.median(ttft[128])
    checks.append(check("timing_requests_completed", not errors, value=len(errors), threshold=0,
                        detail=f"{len(errors)} timing requests failed: {errors[:2]}" if errors else "all timing requests completed"))
    if t127 and t128:
        ratio = t127 / t128
        checks.append(check("ttft_127_vs_128", ratio <= args.ttft_align_ratio, severity="advisory", value=round(ratio, 3),
                            threshold=args.ttft_align_ratio,
                            detail=f"TTFT 127 tok {t127:.0f} ms vs 128 tok {t128:.0f} ms; a slow unaligned prompt "
                                   "usually runs several prefill passes; pad to the bucket inside one pass"))
    else:
        checks.append(check("ttft_127_vs_128", False, severity="advisory", detail="TTFT for 127/128 not measured"))
    cliffs = []
    for a, b in zip(rows, rows[1:]):
        if a["tpot_ms"] and b["tpot_ms"] and b["tpot_ms"] > args.cliff_ratio * a["tpot_ms"]:
            cliffs.append(f"{a['length']}->{b['length']}: {a['tpot_ms']}->{b['tpot_ms']} ms")
    measured = [row for row in rows if row["tpot_ms"]]
    checks.append(check(
        "tpot_cliff", not cliffs and len(measured) == len(rows), severity="advisory", value=len(cliffs),
        threshold=args.cliff_ratio,
        detail=("TPOT jumps > {:g}x: {}; look for a slow long-context kernel path (time box 2 h)".format(args.cliff_ratio, "; ".join(cliffs))
                if cliffs else ("TPOT measured at every length, no cliff" if len(measured) == len(rows)
                                else "TPOT missing at some lengths"))))

    raw_dir = Path(args.model_dir) / RESULT_DIR / TIMING_PROBE
    sweep = chunk_sweep_rows(raw_dir)
    current = args.max_num_batched_tokens or sp.max_num_batched_tokens(log.text(0))
    pick = pick_chunk(sweep, args.itl_bound_ms)
    values = [row.get("max_num_batched_tokens") for row in sweep]
    if len(sweep) < 3:
        checks.append(check("chunk_size_sweep", False, severity="advisory", value=len(sweep), threshold=3,
                            detail=f"chunk sweep has {values}; run --part chunk at 3 max_num_batched_tokens values "
                                   f"(e.g. {'/'.join(map(str, DEFAULT_CHUNK_VALUES))})"))
    elif pick is None:
        checks.append(check("chunk_size_sweep", False, severity="advisory", threshold=args.itl_bound_ms,
                            detail=f"no swept value keeps worst ITL <= {args.itl_bound_ms:g} ms ({values})"))
    else:
        checks.append(check("chunk_size_sweep", current == pick, severity="advisory", value=current, threshold=pick,
                            detail=f"largest value within the ITL bound is {pick}; server runs {current}"
                                   if current != pick else f"server runs the picked max_num_batched_tokens {pick}"))
    lines = [f"lengths {lengths} max_model_len {max_len}",
             "decode ms/token: " + ", ".join(f"{r['length']}:{r['tpot_ms']}" for r in rows),
             f"TTFT 127 {sp.r(t127, 1)} ms vs 128 {sp.r(t128, 1)} ms",
             "chunk sweep: " + ", ".join(f"{r.get('max_num_batched_tokens')}: ttft {r.get('ttft_ms')} worst itl "
                                         f"{r.get('worst_itl_ms')}" for r in sweep),
             f"picked max_num_batched_tokens {pick}; served {current}"]
    return write_result(args.model_dir, TIMING_PROBE, checks, summary_lines=lines, output=args.output,
                        data={"model": model, "max_model_len": max_len, "tpot": rows,
                              "ttft_127_ms": sp.r(t127, 1), "ttft_128_ms": sp.r(t128, 1),
                              "chunk_sweep": sweep, "chunk_pick": pick, "served_max_num_batched_tokens": current,
                              "itl_bound_ms": args.itl_bound_ms})


def run_chunk(args, server: str, model: str, max_len: Optional[int], log: sp.ServerLog) -> Path:
    mnbt = args.max_num_batched_tokens or sp.max_num_batched_tokens(log.text(0))
    if not mnbt:
        raise sp.ProbeError("pass --max-num-batched-tokens (or --server-log with the server's non-default args)")
    prompts = clamp_lengths(sp.parse_csv_ints(args.chunk_prompts), max_len, 64)
    ids = book_ids(args, server, model, sum(prompts) + 256 * (args.decode_users + 1))
    stop = threading.Event()
    background: list[Optional[dict]] = [None] * args.decode_users

    def decoder(i: int) -> None:
        start = sum(prompts) + 256 * i
        background[i] = completion(server, model, ids[start:start + 128], args.decoder_max_tokens, args.request_timeout, stop)

    threads = [threading.Thread(target=decoder, args=(i,), daemon=True) for i in range(args.decode_users)]
    sp.reset_prefix_cache(server)
    for thread in threads:
        thread.start()
    time.sleep(args.decode_warmup_s)  # let the decoders reach steady decode
    measured, errors, offset = [], [], 0
    for length in prompts:
        rec = completion(server, model, ids[offset:offset + length], 4, args.request_timeout)
        offset += length
        if rec.get("error"):
            errors.append(f"{length}: {rec['error']}")
        first = rec["times"][0] if rec.get("times") else rec.get("t_end")
        measured.append({"prompt_tokens": length, "ttft_ms": sp.r(sp.ttft_ms(rec), 1), "window": (rec["t0"], first)})
    stop.set()
    for thread in threads:
        thread.join(timeout=120)
    for row in measured:
        start, end = row.pop("window")
        gaps = [sp.max_gap_ms(rec, start, end) for rec in background if rec]
        gaps = [g for g in gaps if g is not None]
        row["worst_itl_ms"] = sp.r(max(gaps), 1) if gaps else None
    bg_errors = [rec.get("error") for rec in background if rec and rec.get("error")]
    out = {"max_num_batched_tokens": mnbt, "decode_users": args.decode_users,
           "ttft_ms": {str(r["prompt_tokens"]): r["ttft_ms"] for r in measured},
           "worst_itl_ms": max((r["worst_itl_ms"] for r in measured if r["worst_itl_ms"] is not None), default=None),
           "per_prompt": measured, "errors": errors + bg_errors[:3]}
    path = Path(args.model_dir) / RESULT_DIR / TIMING_PROBE / f"chunk_{mnbt}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(f"chunk sweep max_num_batched_tokens={mnbt}: TTFT {out['ttft_ms']} ms, worst ITL {out['worst_itl_ms']} ms "
          f"with {args.decode_users} decoding users -> {path}")
    if out["errors"]:
        print(f"  errors: {out['errors'][:3]}")
    return path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--part", required=True, choices=("accuracy", "timing", "chunk"))
    p.add_argument("--server-url", default="http://localhost:8000")
    p.add_argument("--model-dir", required=True, type=Path)
    p.add_argument("--model", default="")
    p.add_argument("--server-log", type=Path, default=None)
    p.add_argument("--text-file", type=Path, default=None, help="long natural text (.txt or .bz2)")
    p.add_argument("--tt-metal", type=Path, default=None, help="tt-metal checkout to find the readiness book in")
    p.add_argument("--profile", default="default", help="accepted for symmetry with P4; accuracy runs at temperature 0")
    p.add_argument("--profiles-file", type=Path, default=None)
    p.add_argument("--lengths", default="", help=f"accuracy default {DEFAULT_ACCURACY_LENGTHS}; timing default {DEFAULT_TIMING_LENGTHS}")
    p.add_argument("--depths", default="0.1,0.5,0.9")
    p.add_argument("--max-tokens", type=int, default=512, help="accuracy answer cap")
    p.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=False,
                   help="chat_template_kwargs.enable_thinking for the recall question")
    p.add_argument("--decode-tokens", type=int, default=32)
    p.add_argument("--ttft-reps", type=int, default=3)
    p.add_argument("--ttft-align-ratio", type=float, default=TTFT_ALIGN_RATIO)
    p.add_argument("--cliff-ratio", type=float, default=CLIFF_RATIO)
    p.add_argument("--itl-bound-ms", type=float, default=DEFAULT_ITL_BOUND_MS)
    p.add_argument("--max-num-batched-tokens", type=int, default=None, help="the served value; default from --server-log")
    p.add_argument("--chunk-prompts", default="4096,32768")
    p.add_argument("--decode-users", type=int, default=31)
    p.add_argument("--decoder-max-tokens", type=int, default=16384)
    p.add_argument("--decode-warmup-s", type=float, default=10.0)
    p.add_argument("--request-timeout", type=float, default=3600.0)
    p.add_argument("--output", type=Path, default=None)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        server = args.server_url.rstrip("/")
        sp.check_health(server)
        served, max_len = sp.served_model(server)
        model = args.model or served
        log = sp.ServerLog(args.server_log)
        if args.part == "chunk":
            run_chunk(args, server, model, max_len, log)
            return 0
        runner = run_accuracy if args.part == "accuracy" else run_timing
        path = runner(args, server, model, max_len, log)
    except (sp.ProbeError, OSError, ValueError) as exc:
        print(f"ERROR long_context ({args.part}): {exc}")
        return 3
    return print_summary(path)


if __name__ == "__main__":
    sys.exit(main())
