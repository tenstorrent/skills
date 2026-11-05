#!/usr/bin/env python3
"""Replay recorded agent histories against an OpenAI-compatible server (see the agentic-serving survey).

Each history is one JSONL file per agent, one chat-completions request body per line, in the order
the agent sent them. Every request is resent with max_tokens=1, so the run measures what serving
real contexts costs (prefill, prefix-cache reuse, pool pressure) without decode. `--agents` histories
run at once; the rest wait for a free agent.

Cached tokens come from usage.prompt_tokens_details.cached_tokens, which vLLM reports only with
--enable-prompt-tokens-details; without it, hit rate is null. Preemptions are read from the
server's Prometheus /metrics before and after, when it exposes vllm:num_preemptions_total.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def post(url: str, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def preemptions(base_url: str) -> float | None:
    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        with urllib.request.urlopen(root + "/metrics", timeout=10) as response:
            text = response.read().decode()
    except (OSError, urllib.error.URLError):
        return None
    values = [float(line.rsplit(" ", 1)[1]) for line in text.splitlines()
              if line.startswith("vllm:num_preemptions_total")]
    return sum(values) if values else None


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def replay(args) -> dict:
    histories = sorted(Path(args.histories).glob("*.jsonl"))[: args.max_histories or None]
    if not histories:
        raise SystemExit(f"no *.jsonl histories in {args.histories}")
    queue, lock, calls, finished = list(histories), threading.Lock(), [], []
    deadline = time.monotonic() + args.time_cap_s if args.time_cap_s else None
    total = sum(len(load(path)) for path in histories)
    url = args.base_url.rstrip("/") + "/chat/completions"

    def agent() -> None:
        while True:
            with lock:
                if not queue:
                    return
                path = queue.pop(0)
            for index, body in enumerate(load(path)):
                if deadline and time.monotonic() > deadline:
                    return
                body = dict(body, model=args.model or body.get("model"), max_tokens=1, stream=False, temperature=0)
                body.pop("max_completion_tokens", None)
                start = time.monotonic()
                try:
                    usage = post(url, body, args.request_timeout_s).get("usage") or {}
                    error = None
                except (OSError, urllib.error.URLError, ValueError) as exc:
                    usage, error = {}, str(exc)
                details = usage.get("prompt_tokens_details") or {}
                with lock:
                    calls.append({"history": path.name, "call": index, "seconds": time.monotonic() - start,
                                  "prompt_tokens": usage.get("prompt_tokens"),
                                  "cached_tokens": details.get("cached_tokens"), "error": error})
            with lock:
                finished.append(path.name)

    before, start = preemptions(args.base_url), time.monotonic()
    threads = [threading.Thread(target=agent) for _ in range(args.agents)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall, after = time.monotonic() - start, preemptions(args.base_url)
    return summarize(calls, finished, len(histories), total, wall, before, after, args.agents)


def summarize(calls, finished, histories, total, wall, before, after, agents) -> dict:
    ok = [c for c in calls if not c["error"] and c["prompt_tokens"] is not None]
    prompt = sum(c["prompt_tokens"] for c in ok)
    cached_known = all(c["cached_tokens"] is not None for c in ok) and ok
    cached = sum(c["cached_tokens"] for c in ok) if cached_known else None
    last = {}
    for c in ok:
        if c["call"] >= last.get(c["history"], (-1, 0))[0]:
            last[c["history"]] = (c["call"], c["prompt_tokens"])
    final_context = sum(tokens for _, tokens in last.values())
    seconds = sorted(c["seconds"] for c in ok)
    prefilled = prompt - cached if cached is not None else None
    return {
        "agents": agents, "histories": histories, "histories_finished": len(finished),
        "calls_done": len(ok), "calls_total": total, "errors": len(calls) - len(ok),
        "wall_s": round(wall, 1), "prompt_tokens": prompt, "cached_tokens": cached,
        "hit_fraction": round(cached / prompt, 4) if cached is not None and prompt else None,
        "prefilled_tokens": prefilled, "final_context_tokens": final_context,
        "prefilled_per_final_context": round(prefilled / final_context, 2) if prefilled is not None and final_context else None,
        "call_s_median": round(statistics.median(seconds), 2) if seconds else None,
        "call_s_p95": round(seconds[int(0.95 * (len(seconds) - 1))], 2) if seconds else None,
        "call_s_max": round(seconds[-1], 2) if seconds else None,
        "preemptions": after - before if before is not None and after is not None else None,
        "calls": calls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible base, e.g. http://127.0.0.1:8000/v1")
    parser.add_argument("--histories", required=True, help="directory of <agent>.jsonl request histories")
    parser.add_argument("--agents", type=int, required=True, help="histories replayed at once")
    parser.add_argument("--model", default="", help="override the model name in each request")
    parser.add_argument("--max-histories", type=int, default=0)
    parser.add_argument("--time-cap-s", type=float, default=0, help="stop starting calls after this long")
    parser.add_argument("--request-timeout-s", type=float, default=3600)
    parser.add_argument("--out", required=True, help="JSON summary with per-call rows")
    args = parser.parse_args()
    result = replay(args)
    Path(args.out).write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "calls"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
