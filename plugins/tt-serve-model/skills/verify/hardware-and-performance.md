# Hardware Utilization & Performance

V7–V12. Requires the container id (`$CID`, from `tt:launch`) and Discovery's
idle baseline (D12).

## Commands

```bash
export EP=${ENDPOINT:-http://localhost:8000} MODEL="${MODEL:?}"   # the python blocks read both from the environment

echo "==V7/V9"; docker logs "$CID" 2>&1 | grep -vE 'loggers\.py|Avg prompt throughput' \
  | grep -iE "Attempting to open mesh|multidevice with|Set MESH_DEVICE|Loading [0-9]+ transformer layers|num_gpu_blocks|GPU KV cache size|Maximum concurrency" | head -12

echo "==V8a  engine counters — the only sound answer"; docker logs --tail 200 "$CID" 2>&1 \
  | grep -oE 'Avg generation throughput: [0-9.]+ tokens/s, Running: [0-9]+ reqs' | tail -3

echo "==V8b  telemetry — context only, never the verdict"
TTS=$(command -v tt-smi || ls -1 ~/.tenstorrent-venv/bin/tt-smi ~/tt-smi/.venv/bin/tt-smi 2>/dev/null | head -1)
[ -x "$TTS" ] && $TTS -s --snapshot_no_tty > "$SCRATCH/tt-smi-load.json" && python3 - "$SCRATCH/tt-smi-load.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
for i,x in enumerate(d['device_info']):
    t=x['telemetry']
    print(f"chip {i} power={t['power'].strip()}W temp={t['asic_temperature']}C aiclk={t['aiclk'].strip()}MHz fmax={x['limits']['asic_fmax']}")
PY

echo "==V10"; python3 - <<'PY'
import json,time,urllib.error,urllib.request,os
ep,model=os.environ.get('EP','http://localhost:8000'),os.environ['MODEL']
body=json.dumps({"model":model,"prompt":"Once upon a time","temperature":0,"max_tokens":250,"ignore_eos":True}).encode()
t=time.time()
try:
    r=json.load(urllib.request.urlopen(urllib.request.Request(ep+"/v1/completions",body,{'Content-Type':'application/json'}),timeout=60))
except (urllib.error.URLError, TimeoutError) as e:
    print(f"V10=request failed: {e} — server may be the thing that's broken, see V1 before trusting anything else"); raise SystemExit
el=time.time()-t; n=r['usage']['completion_tokens']
print(f"{n} tokens in {el:.1f}s = {n/el:.1f} tok/s  (finish={r['choices'][0]['finish_reason']})")
print(f"V10_TOKPS={n/el:.2f}")  # feeds V11's batching verdict below
PY

echo "==V11"; python3 - <<'PY'
import json,time,urllib.error,urllib.request,os,subprocess
from concurrent.futures import ThreadPoolExecutor
ep,model,N=os.environ.get('EP','http://localhost:8000'),os.environ['MODEL'],16
v10=float(os.environ.get('V10_TOKPS','0'))  # set from V10's printed V10_TOKPS, 0 if V10 wasn't run
body=json.dumps({"model":model,"prompt":"Once upon a time","temperature":0,"max_tokens":128,"ignore_eos":True}).encode()
def one(_):
    try:
        t=time.time()
        r=json.load(urllib.request.urlopen(urllib.request.Request(ep+"/v1/completions",body,{'Content-Type':'application/json'}),timeout=120))
        return time.time()-t, r['usage']['completion_tokens'], None
    except (urllib.error.URLError, TimeoutError) as e:
        return None, 0, str(e)
t0=time.time()
with ThreadPoolExecutor(N) as x: res=list(x.map(one,range(N)))
errs=[e for _,_,e in res if e]
if errs:
    print(f"V11=request(s) failed: {len(errs)}/{N}, e.g. {errs[0]} — not a batching result"); raise SystemExit
wall=time.time()-t0; lat=[a for a,_,_ in res]; tok=sum(b for _,b,_ in res)
agg=tok/wall
print(f"{N} req -> {tok} tok in {wall:.1f}s = {agg:.1f} tok/s aggregate")
print(f"latency min {min(lat):.1f}s max {max(lat):.1f}s spread {max(lat)-min(lat):.1f}s")
# A zero/small latency spread alone does not prove batching -- a serialised
# server processing N identical requests back-to-back also rounds to a flat
# spread. Corroborate with throughput scaling vs V10's single-stream baseline
# and, where available, the engine's own "Running: N reqs" counter (V8).
if v10 > 0:
    ratio = agg / v10
    print(f"throughput ratio vs V10 single-stream ({v10:.1f} tok/s) = {ratio:.1f}x")
    # A fully serialised server processing N identical requests back-to-back
    # still totals ~1x single-stream throughput (same work, done sequentially).
    # Real batching on constrained hardware rarely reaches N x, but comfortably
    # clears 1x -- the 2026-09-22 run measured 3.18x at N=16 under genuine
    # continuous batching, confirmed independently via the engine counter.
    verdict = "continuous batching" if ratio > 1.5 else "serialised" if ratio < 1.3 else "inconclusive from throughput alone"
else:
    verdict = "inconclusive: V10 not run, no single-stream baseline to compare against"
cid = os.environ.get('CID')
if cid:
    tail = subprocess.run(["docker","logs","--tail","20",cid], capture_output=True, text=True).stdout
    running = [l for l in tail.splitlines() if "Running:" in l and "reqs" in l]
    if running:
        print(f"engine counter during burst: {running[-1].strip()}")
print(f"verdict={verdict}")
PY

echo "==V12"; python3 - <<'PY'
import json,time,urllib.error,urllib.request,os
ep,model=os.environ.get('EP','http://localhost:8000'),os.environ['MODEL']
budget=int(os.environ.get('V12_MAX_TOKENS','700'))   # reasoning model: 600+, see V17
prompt=("The following list must be remembered. "+"alpha bravo charlie delta echo foxtrot "*300
        +"\nQuestion: what is 2+2? Answer with the number only.")
body=json.dumps({"model":model,"messages":[{"role":"user","content":prompt}],
                 "temperature":0,"max_tokens":budget}).encode()
t=time.time()
try:
    r=json.load(urllib.request.urlopen(urllib.request.Request(ep+"/v1/chat/completions",body,{'Content-Type':'application/json'}),timeout=180))
except (urllib.error.URLError, TimeoutError) as e:
    print(f"V12=request failed or timed out: {e}"); raise SystemExit
el=time.time()-t; u=r['usage']; c=r['choices'][0]
print(f"prompt_tokens={u['prompt_tokens']} completion_tokens={u['completion_tokens']} in {el:.1f}s")
print(f"finish={c['finish_reason']} content={c['message'].get('content')!r:.120}")
PY
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| V7 | All ASICs in the mesh? | grep container logs for `multidevice with <n> devices and grid` | matches the expected grid for this device string (e.g. `(1,4)` for p300x2), else flag |
| V8 | Doing real work? | the engine's own counters (`Avg generation throughput`, `Running: <n> reqs`); telemetry only as context | `<n> reqs running, <n> tok/s generation (telemetry <power>W / <aiclk>MHz)` or `idle: 0 reqs, 0 tok/s` |
| V9 | KV-cache layers allocated | `Loading <n> transformer layers` in the log; cross-check `config.json`'s `num_hidden_layers`; blocks × block size | match or mismatch, name both numbers |
| V10 | Single-stream throughput | `usage.completion_tokens` ÷ wall time, batch 1, temp 0, `ignore_eos` | `<tokens> in <seconds>s = <tok/s>` |
| V11 | Concurrency: batching or serial? | aggregate tok/s vs V10's single-stream baseline, corroborated by the engine's `Running: N reqs` counter (V8) — latency spread alone is supporting evidence, never the verdict | `<n> req → <tok/s> aggregate, <ratio>x V10 → <continuous batching \| serialised \| inconclusive>` |
| V12 | Long prefill works? | `usage.prompt_tokens` and wall time on a ~1700–2048 token prompt, plus whether the answer was reached | `<n> prompt tokens in <n>s, <answer reached \| budget consumed before the answer>` or hang/timeout |

## Rules

- **V8 cannot be answered from telemetry at all.** Measured on this host,
  2026-09-22, all four states:

  | State | Power | AICLK |
  |---|---|---|
  | post-reset idle (run 1) | 18–35 W | 800 MHz |
  | post-reset idle (run 2, clean) | 15–17 W | 800 MHz |
  | server up, **0 requests in flight** (11 h idle) | 16–75 W | 800 / 1350 MHz |
  | server under real load (run 1) | 72–81 W | 1343–1350 MHz |
  | server under real load (run 2, 16 parallel) | **56–61 W** | 1350 MHz |
  | wedged, **no container at all** (post-crash) | 60–82 W | 1343–1350 MHz |

  An idle-but-running server, a loaded server and a wedged mesh with nothing
  running are indistinguishable on power *and* on AICLK. Only the post-reset
  state is separable. Worse than ambiguous: run 2 under 16 concurrent
  requests drew **less** power (56–61 W) than the wedged mesh did with
  nothing running at all (60–82 W). A power threshold would have called the
  wedged box "working" and the working box "idle". An earlier revision of this row proposed AICLK as "the
  discriminator" on the strength of the load-vs-post-reset pair alone; the
  two rows added later refute it. Read the engine's own counters — `Running:
  <n> reqs` and `Avg generation throughput` — which are emitted every 10 s
  and are unambiguous. Telemetry goes in the answer as context, never as the
  verdict.
- V8: when there is no container to read counters from, the honest answer is
  `unknown: no engine to report; telemetry cannot distinguish wedged from
  loaded` — plus a reset verdict from `tt:discover` D14.
- V8's telemetry capture is the same as `discover/boards.md` § Capture —
  `-s --snapshot_no_tty` with the binary located explicitly. A bare
  `tt-smi -s` writes a TTY table, not JSON, and tt-smi is often not on `PATH`.
- Watch for one chip out of line: chip 1 on this host sat at 16 W / 800 MHz
  in every reading while chips 0/2/3 were at 1343–1350 MHz. Report a chip
  that disagrees with its neighbours rather than averaging it away.
- V9: the layer count lives in `Loading <n> transformer layers (indices=[...])`.
  The older grep (`multidevice with|Set MESH_DEVICE|kv.?cache`) never matched
  that line, so the row could not answer its own question.
- **V7/V9: filter the engine-stats noise before grepping.** `kv.?cache`
  matches the `GPU KV cache usage` line that `loggers.py` prints every 10
  seconds, so on a server that has been up for hours a `tail` of the grep
  returns nothing but those. Exclude `loggers.py` / `Avg prompt throughput`
  and take `head` — the mesh and layer lines are emitted once, at startup.
- **V10 must read `usage.completion_tokens`.** `len(text.split())` is a word
  count, not a token count; on the 2026-09-22 run the two were ~30% apart.
- V11 is only meaningful where the model's spec entry permits `batch > 1`
  (Model Retrieval R12) — running it against a batch-1-only model and reporting
  "no batching" would be a false finding.
- **V11 needs `ignore_eos` and an identical prompt for every request.**
  Without it, requests stop at different lengths and a latency spread no
  longer means serialisation — the proof is confounded by the content. The
  old version also only printed 16 interleaved `time` blocks and computed no
  aggregate at all.
- **A flat latency spread does not by itself prove batching.** A serialised
  server working through N identical requests back-to-back also produces
  nearly equal per-request latencies once rounded — spread alone cannot tell
  "16 requests batched together" from "16 requests queued one after another
  that each happen to take about the same time". Compare aggregate tok/s
  against V10's single-stream figure instead: aggregate near N× single-stream
  is batching, aggregate near 1× is serialisation. Corroborate with the
  engine's own `Running: N reqs` counter from the container log during the
  burst (the same signal V8 reads) wherever `$CID` is available.
- **V12 must actually send the prompt.** The previous version only wrote
  `$SCRATCH/long-prompt.txt` and never issued a request, so the row could
  never fail. Also `'word '*2048` is 2048 *words*, not tokens.
- V12 on a reasoning model: a small token budget is consumed inside `<think>`
  and the answer is never reached — on 2026-09-22 a 16-token budget produced
  no answer despite a healthy 1.1 s prefill. Use 600+ (see V17) or
  `enable_thinking:false`. Prefill timing and answer correctness are two
  separate verdicts; report both.

## Status

| Row | State |
|---|---|
All rows re-run 2026-09-22 against run 2. Raw output:
`run-2026-09-22/artifacts/run2-v7-v10.out`, `run2-v11-v8.out`, `run2-v12-v17.out`.

| Row | State |
|---|---|
| V7 | **TESTED twice**, two separate launches: `Attempting to open mesh device with grid shape (1, 4)` → `multidevice with 4 devices and grid (1, 4) is created` |
| V8 | **TESTED.** Engine counters are decisive: `Running: 16 reqs, Avg generation throughput: 187.1 tokens/s` under load vs `Running: 0 reqs, 0.0 tokens/s` idle. Telemetry under load measured **56–61 W** — *lower* than the wedged state's 60–82 W, so power is not merely ambiguous here, it is inverted |
| V9 | **TESTED** — the corrected grep returns all of it: `Loading 64 transformer layers`, `num_gpu_blocks_override=689`, `GPU KV cache size: 551,200 tokens`, `Maximum concurrency 2.10x`. Matches `config.json`'s 64 |
| V10 | **TESTED** — 250 tokens in 10.6 s = **23.5 tok/s** via `usage.completion_tokens`. The word count on the same response was 206, **18% low** — the old counter's error, measured |
| V11 | **TESTED, and the earlier number was wrong.** With `ignore_eos`: 16 req → 2048 tok in 27.4 s = **74.7 tok/s** aggregate, **3.18x** V10's 23.5 tok/s single-stream baseline → continuous batching (latency spread was 0.0 s too, but that alone would not have been proof). The morning's 98.6 tok/s came from requests stopping early without `ignore_eos`. The batching verdict survives; the throughput figure does not |
| V12 | **TESTED** — 2733 prompt tokens in 15.3 s, `finish_reason: stop`, answer reached (`'\n\n4'`). The row that previously sent no request at all now passes |

### The engine died on V10's request — 2026-09-22 18:08:35 UTC

Reproduction of the corrected V10 was the first request the server had seen
in roughly 11 hours. The engine logged
`Prefilling 1 user(s) into slots [0] (TP batched masked-bucket)` and then:

```
Signal: Bus error (7) / Signal code: Non-existent physical address (2)
  tt::umd::write32_to_device
  tt::tt_metal::SystemMemoryManager::fetch_queue_write
  tt::tt_metal::program_dispatch::write_program_command_sequence
  tt::tt_metal::distributed::FDMeshCommandQueue::enqueue_mesh_workload
→ EngineCore_DP0 died unexpectedly → EngineDeadError → container Exited (0)
```

Full excerpt: `run-2026-09-22/artifacts/crash-2026-09-22T18-08-35.log`.

The request was a plain `/v1/completions`, `temperature: 0`, 250 tokens,
`ignore_eos` — the same shape that ran fine that morning. This is a device
write failing at the UMD layer, not a client-side or parameter error. It is a
**new failure signature**, and it happens *after* `/health` has been 200 for
hours, which no existing row covers: `launch`'s signatures only apply before
`/health`, and `verify` assumes the server stays up while it is questioned.
Proposed as **V26 — does the server survive an idle period?** and mirrored as
a new post-`/health` signature in `launch/diagnose.md`.

Golden-dataset figures that are *not* this model/image: 11.4 tok/s for V10 is
Qwen3-32B on 0.17.0, about 2× off what this model does on 0.20.0. Never quote
a throughput number without its model and image tag.
