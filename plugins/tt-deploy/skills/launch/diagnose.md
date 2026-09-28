# Launch Diagnostics

L1, L2, L4, L8, L9, L10 — everything read from the launching container's own
logs and state. One capture, every question here reads it.

For the launch command itself — mandatory flags, override selection and
safety — see `override.md`.

## Capture

```bash
CID=${CONTAINER:-$(docker ps -a --format '{{.CreatedAt}}\t{{.ID}}\t{{.Image}}' \
  | grep -E 'vllm-tt-metal|tt-inference-server' | sort -r | head -1 | cut -f2)}
[ -n "$CID" ] || { echo "no matching container found"; exit 1; }

docker inspect "$CID" --format 'status={{.State.Status}} exit={{.State.ExitCode}} started={{.State.StartedAt}} image={{.Config.Image}}' \
  > "$SCRATCH/launch-state.txt"

# Full log to file, but NEVER read it unbounded — see Rules.
docker logs --timestamps "$CID" > "$SCRATCH/launch-log.txt" 2>&1
docker logs --timestamps --tail 200 "$CID" > "$SCRATCH/launch-tail.txt" 2>&1
docker ps --format '{{.Names}}\t{{.Ports}}\t{{.Status}}' > "$SCRATCH/docker-ps.txt"

cat "$SCRATCH/launch-state.txt"
grep -vE 'loggers\.py|Avg prompt throughput' "$SCRATCH/launch-tail.txt" | tail -n 5
```

`launch-tail.txt` is what L1 reads. A healthy server emits an engine-stats
line every 10 s, so on a long-running container the full log reaches tens of
thousands of lines and a plain `tail` of a grep returns nothing but stats.

## Stage markers (L2)

Derive the stage list from the log; the static chain is only a fallback.

```bash
grep -nE 'Downloading weights|Fetching [0-9]+ files|Set MESH_DEVICE|Set TT_CACHE_PATH|Attempting to open mesh|multidevice with|Loading [0-9]+ transformer layers|GPU KV cache size|skipping background trace capture|[Cc]apturing trace|Application startup complete|Uvicorn running' \
  "$SCRATCH/launch-log.txt" | grep -vE 'loggers\.py'
```

Observed, 2026-09-22 / p300x2 / 0.20.0 (Qwen3.6-27B), **two launches of the
same model on the same box**, differing only in what was already cached:

| Stage | Marker | Run 1 — cold weights, warm tensor | Run 2 — both warm |
|---|---|---|---|
| spec resolve, MESH_DEVICE, TT_CACHE_PATH | `Set MESH_DEVICE to P300x2` | 4 s | 4 s |
| weight download (29 files / 15 shards) | `Fetching 29 files` | 13 m 49 s | **0 s (cached)** |
| builtin-warmup branch | `skipping background trace capture` | — | 5 s |
| vLLM engine init | plugin/platform activation | 14 m 11 s | — |
| open mesh | `multidevice with 4 devices and grid (1, 4) is created` | 25 m | **36 s** |
| load layers | `Loading 64 transformer layers` | 25 m | 39 s |
| KV cache | `GPU KV cache size: 551,200 tokens` | 26 m | 1 m 23 s |
| serving | `/health` → 200 | 28 m | **4 m 37 s** |

Run 2 is `artifacts/phases-run2.log`; run 1 is in `STATUS.md`.

Branch: if the spec entry has `has_builtin_warmup: true`, the log says
`skipping background trace capture` and **there is no trace-capture stage**.
The static chain that lists one does not reproduce for such a model. There is
also no distinct "shard read" stage.

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| L1 | Slow or hanging? | last non-stats line in `launch-tail.txt` vs now, against the stage table | `<stage> in progress, <n>s since last line (normal)` or `no output for <n>s at <stage> — investigate` |
| L2 | All launch steps | stage markers above; static chain only as fallback | ordered stage list with elapsed per stage, and the `has_builtin_warmup` branch stated |
| L4 | Failure class | grep `$SCRATCH/launch-log.txt` against the signatures below | one of the named signatures + fix, or `unknown failure: <tail>` |
| L8 | Timing / where it goes | elapsed since `started=` mapped onto the stage table | per-phase elapsed table, not a single range |
| L9 | Anything displaced | `$SCRATCH/docker-ps.txt` **vs the pre-launch baseline** (D8/D13) | `none` / `<service> moved <old>→<new>` / `unknown: no pre-launch baseline` |
| L10 | Silent fallbacks and env overrides | the override grep below | list of overrides with the value actually used, or `none` |

## Failure signatures (L4)

**Original 4 — from the golden dataset:**

```bash
LOG="$SCRATCH/launch-log.txt"
grep -q "No model spec found" "$LOG" && \
  echo "stale registry (3-14s) — refresh MODEL_SPECS_JSON_PATH or the image"
grep -qE "Timed out waiting for active ethernet core" "$LOG" && \
  echo "wedged inter-chip links — tt:run recovery (tt-smi -r), do not retry blind"
grep -qE "Bind for 0\.0\.0\.0:8000 failed" "$LOG" && \
  echo "port collision — see L9"
grep -qiE "out of memory|\bOOM\b|CUDA out of memory" "$LOG" && \
  echo "OOM — check batch/context against the spec ceiling from Model Retrieval"
```

**6 more — found live on a real QB2, 2026-09-17, not in the original set:**

```bash
grep -qE 'exec: "--model": executable file not found' "$LOG" && \
  echo "BROKEN ENTRYPOINT — this image's docker-entrypoint.sh does not prepend" \
       "'python run_vllm_api_server.py' before CLI flags. Workaround: pass" \
       "'bash -c \"source \$PYTHON_ENV_DIR/bin/activate && python run_vllm_api_server.py <flags>\"'" \
       "instead of the bare flags. Report as a product bug, not a config error."
grep -q "JWT_SECRET is not set" "$LOG" && \
  echo "run.py --docker-server refuses to start non-interactively without" \
       "JWT_SECRET, unless --no-auth is passed. Not a hardware problem."
grep -qi "Enter your HF_TOKEN" "$LOG" && \
  echo "run.py requires a VALIDATED HF_TOKEN even for a fully public," \
       "ungated model (confirms M16). A real token is required regardless" \
       "of whether this specific model needs one."
grep -q "stat: cannot statx" "$LOG" && \
  echo "CACHE_ROOT is empty inside the container. If launching by hand" \
       "(bypassing run.py), you must set -e CACHE_ROOT=<path matching the" \
       "--volume mount destination> explicitly — run.py normally wires this" \
       "but a hand-built docker run does not inherit it."
grep -q "ModuleNotFoundError: No module named 'vllm'" "$LOG" && \
  echo "The launch command used the container's bare 'python', not the" \
       "venv at \$PYTHON_ENV_DIR. Must 'source \$PYTHON_ENV_DIR/bin/activate'" \
       "first."
grep -q "TT_MODEL_SPEC_JSON_PATH environment variable is not set" "$LOG" && \
  echo "The runtime model-spec path is not set. On 0.20.0 the image already" \
       "carries MODEL_SPECS_JSON_PATH=/home/container_app_user/model_specs/" \
       "model_spec.json and a hand-built docker run works as-is; older images" \
       "(0.10.0) hit this because run.py generated the file per run. Check the" \
       "image's own ENV before assuming run.py is required."
```

**1 more — found 2026-09-22, and it fires AFTER `/health` has been 200:**

```bash
grep -qE "Signal: Bus error|Non-existent physical address" "$LOG" && \
  echo "DEVICE WRITE FAULT (SIGBUS) in tt::umd::write32_to_device — the engine" \
       "died on a device write, not on a client request. Seen on the first" \
       "request after ~11 h idle: 'Prefilling 1 user(s)' then SIGBUS," \
       "EngineCore died, container Exited (0). The mesh is left wedged;" \
       "tt:run recovery (tt-smi -r) before any relaunch. Not a config error."
```

## Silent fallbacks and env overrides (L10)

```bash
grep -vE 'loggers\.py' "$LOG" | grep -iE \
  'setting .* to [0-9]+ for compatibility|Unknown model .* on device|ARCH_NAME .*(->|→)|Set MESH_DEVICE|overriding|falling back|not supported.*disabl' \
  | sort -u | head -20
```

Observed 2026-09-22 / p300x2 / 0.20.0 — three hits, none of them a failure
signature, all of them behaviour-changing:

- `Unknown model Qwen3.6-27B on device P150x4, setting MAX_PREFILL_CHUNK_SIZE
  to 4 for compatibility` — the stack believes this p300x2 host is a P150x4
  and silently takes the smallest prefill chunk, while the same log line
  suggests up to 128. A performance fallback, not an error. **File upstream.**
- `ARCH_NAME wormhole_b0 → blackhole`
- `MESH_DEVICE P300x2 → (1,4)`

Before L10 existed these were only caught by `verify` V22, i.e. after
`/health` — too late to influence the launch.

## Rules

- L1: silence is not failure. Kernel JIT/tilize is legitimately quiet for
  minutes at a time — always check the stage table before calling it stuck.
- **L1 must read a bounded capture.** `docker logs` on a healthy server grew
  to tens of thousands of engine-stats lines in 35 minutes; use `--tail` (or
  `--since`) and filter `loggers.py` before taking the last line, or L1
  reports the stats line as "the last thing that happened" forever.
- L4: signatures are mostly mutually exclusive; if two match, report both and
  let the agent judge which is proximate. A match against none of them is
  itself a new signature to add here, not a dead end.
- **L4: two spec-path variables exist and they are not the same.** The image
  ENV on 0.20.0 is `MODEL_SPECS_JSON_PATH`; the signature above greps
  `TT_MODEL_SPEC_JSON_PATH`, which is what the 0.10.0 runtime raised. Report
  whichever the log printed; never normalise one into the other. And the old
  advice "prefer run.py over reconstructing the invocation by hand" is wrong
  on 0.20.0 — the hand-built `docker run` works, while `run.py` blocks on a
  validated HF_TOKEN. Pin every remedy to the image release it was tested on.
- **L8: report the per-phase table, not a range.** Two launches of the same
  model on the same box bracket the recorded "8–22 min cold / 6–8 min warm"
  from both sides: **29 min** with only the tensor cache warm, and
  **4 m 37 s** with both caches warm. Neither is inside the recorded range.
  "Warm" is two independent variables — the weights cache and the tensor
  cache — and a single range cannot express that. Ask *which* caches are
  warm before predicting anything, and state the elapsed/expected pair per
  phase rather than a bare verdict of "too slow".
- L8: the phase that dominates is not the one you would guess. In run 1 mesh
  assembly took 10 m 48 s; in run 2 it took 6 s. Weight download, the phase
  everyone expects to dominate, was 13 m 49 s cold and 0 s warm. Both the
  biggest and the second-biggest cost are cache-dependent, which is why a
  fixed range is the wrong shape of answer.
- **L9 cannot answer its own question from `docker ps` alone.** Naming a
  displaced service needs the pre-launch baseline from `tt:discover` D8/D13.
  Absent that, the honest answer is `unknown: no pre-launch baseline` — not
  `none`. TT-Studio moving 8000→8001 is expected and benign; any *other*
  service displaced is worth surfacing unprompted.

## Status

**PARTIALLY TESTED.** The 6 signatures found 2026-09-17 were reproduced live
on a real QB2 (Qwen3-32B on p300x2, by hand and via `run.py`); their grep
patterns match the exact strings from those logs.

Added 2026-09-22 from a full launch of Qwen3.6-27B on p300x2 / 0.20.0: the
stage table (measured), L8's per-phase figures (measured), L10's three hits
(measured), L4's `TT_MODEL_SPEC_JSON_PATH` signature (fired for real on the
earlier 0.10.0 attempt) and the SIGBUS signature (fired at 18:08:35, full
excerpt in `../../run-2026-09-22/artifacts/crash-2026-09-22T18-08-35.log`).

Still untested: the original 4 golden-dataset signatures, L1's answer format,
L6/L7 (see `override.md`), and L9 — which has never been run with a real
pre-launch baseline to compare against.
