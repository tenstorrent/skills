# Debug Elimination & Provenance

V19–V25. Invoked only when an earlier check (V3–V17) failed and a root cause
is needed — not part of the default happy-path pipeline.

## Commands

```bash
echo "==V19"; curl -s "$EP/v1/completions" -H 'Content-Type: application/json' -d '{"model":"'"$MODEL"'","prompt":"'"$FAILING_PROMPT"'","temperature":0}'
echo "==V20"; curl -s "$EP/v1/completions" -H 'Content-Type: application/json' -d '{"model":"'"$MODEL"'","prompt":"'"$FAILING_PROMPT"'"}'
echo "==V21"; diff <(python3 -c "import json;print(json.dumps(json.load(open('$OVERRIDE'))['model_specs']['$MODEL'],sort_keys=True))") \
                  <(python3 -c "import json;print(json.dumps(json.load(open('$SCRATCH/spec.json'))['model_specs']['$MODEL'],sort_keys=True))")

echo "==V22"; docker logs "$CID" 2>&1 \
  | grep -vE "loggers\.py|Avg prompt throughput|Triton is installed|NVML Shared Library|No module named 'libtpu'|No module named 'amdsmi'|HF_TOKEN to enable higher rate limits|leaked (function|instance)" \
  | grep -iE 'warn|fallback|mismatch|\bpcc\b|dtype|not supported|unsupported|ignor|degrad|accuracy|unsafe|active trace|setting .* to [0-9]+ for compatibility|overrid|deprecat' \
  | sort -u | head -40

echo "==V23"; E=$(ls $SCRATCH/entry_*.json 2>/dev/null | head -1)
if [ -z "${DEV:-}" ]; then
  echo "V23 requires \$DEV (Resolve: --device or D9, uppercase) — not set"
elif [ -n "$E" ]; then
  T=$(python3 -c "import json;t=json.load(open('$E'))['model_type'].lower();print('tts' if t=='text_to_speech' else t)")
  N=$(python3 -c "import json;print(json.load(open('$E'))['model_name'])")
  DOCP="docs/model_support/$T/${N}_${DEV,,}.md"
  echo "doc=$DOCP"
  curl -sf "https://raw.githubusercontent.com/tenstorrent/tt-inference-server/main/$DOCP" \
    || echo "no page at $DOCP — list docs/model_support/$T/ for other _<dev>.md files for this model"
else echo "V23 requires tt:retrieve's captured entry (model_name + model_type)"; fi

echo "==V24"; curl -s "https://api.github.com/search/issues?q=repo:tenstorrent/tt-metal+${SYMPTOM// /+}"
echo "==V25"; curl -s "https://api.github.com/repos/tenstorrent/tt-metal/compare/${FIX_SHA}...${TT_METAL_SHA}" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('status'), d.get('ahead_by'), d.get('behind_by'), d.get('message',''))"
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| V19 | Is it sampling? | re-run the failing request at `temperature: 0` | reproduces → not sampling; else flag as sampling-sensitive |
| V20 | Is it the chat template? | re-run via `/v1/completions` instead of `/v1/chat/completions` | reproduces → not the template |
| V21 | Is it my own override? | byte/structural diff of the injected spec entry vs upstream | `matches upstream` or names the diverging field |
| V22 | Did the server warn quietly? | filtered grep of the container log | de-duplicated list of hits, or `none` |
| V23 | Does the impl's own README validate this hardware? | fetch `docs/model_support/<model_type>/<model_name>_<dev>.md` | `validated on <DEV>` / `no page for <DEV> — validated elsewhere only, treat with suspicion` / `requires $DEV — not set` |
| V24 | Known upstream bug? | GitHub issue search for the symptom | list of issue numbers + state, or `none found` |
| V25 | Does a published image contain the fix? | GitHub compare API, fix-SHA...**tt-metal**-SHA | `status: ahead/identical → contained` or `status: diverged → NOT contained`. NEVER decide this by comparing version-tag strings. |

## Rules

- V19 → V20 → V21 is a **strict elimination order**. Don't skip ahead to
  "must be my override" before ruling out sampling and template — the golden
  dataset exists precisely because that shortcut wastes debugging time.
- V22's keyword list is deliberately broad; a hit doesn't mean the symptom is
  explained, only that it's worth reading in full before concluding anything.
- **V22 needs a noise filter or it is unusable.** The raw grep on a real log
  returns a wall of Triton / NVML / libtpu / amdsmi / HF_TOKEN lines that are
  present on every healthy run, plus the 10-second engine-stats line. Exclude
  them and `sort -u`; what remains on 2026-09-22 was six real warnings.
- **V22 was missing the dataset's own example.** `unsafe|active trace` — from
  gpt-oss's "Allocating device buffers is unsafe due to the existence of an
  active trace" — was not in the keyword list although the golden dataset
  cites it. Added, along with `setting ... to <n> for compatibility`, which is
  what caught the P150x4 misdetection.
- The most valuable V22 hit on this host is not an error:
  `Unknown model Qwen3.6-27B on device P150x4, setting MAX_PREFILL_CHUNK_SIZE
  to 4 for compatibility` — a silent performance fallback on a host that is
  actually p300x2, where the log itself suggests up to 128. Also seen:
  `ARCH_NAME wormhole_b0 → blackhole`, async scheduling disabled, a
  suboptimal fabric packet size, and deprecated `ttnn.all_gather` args.
- **V23's URL must be built from the spec entry, not from `$MODEL`.**
  `${MODEL}` is an HF repo id (`Qwen/Qwen3.6-27B`); interpolating it puts a
  slash inside the path and the fetch can never succeed. Use the entry's
  `model_name` (`Qwen3.6-27B`) plus the `model_type` directory — and note the
  directory is **not always `llm/`** (`text_to_speech` maps to `tts`). This is
  the same construction `retrieve/spec.md` already uses; keep the two in step.
- **V25: the tag carries two hexes and they are not interchangeable.** For
  `0.20.0-de59f8a-03fa3af` the engine reported `v0.1.dev14187+g03fa3af2e`,
  so the **second** field is the vLLM commit and the **first** is tt-metal.
  Ancestry is a tt-metal question, so compare against the first
  (`de59f8a`) — the spec entry's `tt_metal_commit` states it explicitly and
  is the safer source than parsing the tag. `retrieve/image.md` says "the
  tag's second dash field", which reads ambiguously; both files must say the
  same thing.
- V24/V25: unauthenticated GitHub API is rate-limited (~10 req/min for
  search, 60/hr general). Cache results in `$SCRATCH` keyed by symptom/SHA
  pair — don't re-query the same question twice in one session. Budget ≤6
  GitHub calls per run (1 search, ≤3 compare, 1 issue-comments fetch) — same
  discipline as `tt:retrieve`'s R13/R15. `GH_TOKEN`, if set, lifts the limit;
  NEVER echo it or include it in a printed command.
- V25 is the one row in the whole dataset with an explicit "never do it the
  easy wrong way" warning attached (never compare by version-string order) —
  treat that as load-bearing, not a stylistic preference.

## Status

Re-run 2026-09-22 against run 2. Raw output:
`run-2026-09-22/artifacts/run2-v6-v25.out`.

| Row | State |
|---|---|
| V19–V21 | **UNTESTED** — the elimination chain needs a failure to eliminate, and no generation check failed; no override was in play |
| V22 | **TESTED** with the extended keywords and the noise filter. Hits: the P150x4 misdetection **plus** the line right after it (`Try setting MAX_PREFILL_CHUNK_SIZE to larger powers of 2 up to e.g. 128`), `ARCH_NAME → blackhole`, `MESH_DEVICE → (1, 4)`, async scheduling unsupported, non-public `TTScheduler`, sampling params overridden by `generation_config.json`. No PCC / dtype / accuracy hits. The filter is what makes this readable — unfiltered the same grep returns a wall |
| V23 | **TESTED** — the entry-derived URL resolves: `docs/model_support/llm/Qwen3.6-27B_p300x2.md`, HTTP 200, 52 lines, `Model Status 🛠️ Experimental`, `Max Batch Size 32`, `Max Context Length 262144`, and `--tt-device p300x2` in the documented command → **validated on P300X2**. The old `${MODEL}`-based URL could never have resolved |
| V24 | **UNTESTED** as a search. Two issues drafted from this run instead: `upstream-issues/01-p150x4-misdetection.md` and `02-sigbus-after-idle.md` |
| V25 | **Field order TESTED.** Engine reports `v0.1.dev14187+g03fa3af2e`; the spec entry says `vllm_commit=03fa3af`, `tt_metal_commit=de59f8a`. The hex in the engine's version string is the **vLLM** one, i.e. the tag's *second* field — so ancestry must use the *first*, `de59f8a`. The compare API call itself still needs a real fix SHA to exercise |
