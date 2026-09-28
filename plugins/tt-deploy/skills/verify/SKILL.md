---
name: verify
description: "Verify a running vLLM/TT-inference-server model is actually healthy, correct, and performant — health, served model/context, generation correctness (arithmetic, sequential state, code), mesh/hardware utilization, throughput and batching, reasoning-model quirks, and root-cause elimination when something is wrong. Deterministic test recipes against a live endpoint. Use once /health returns 200."
metadata:
  layer: tool
---

# TT Verify

## Purpose

Produce the Verification table (V1–V25) against a live, already-launched
server, and narrow down the root cause when a check fails. Every answer comes
from an actual request/response, a log grep, or a listed static rule — never
from assuming the server works because it answered *something*.

## When to Invoke

Invoke `tt:verify` (Skill tool) when:

- `tt:launch` reports `/health` → 200 (the hand-off point).
- The user asks: *"is it working"*, *"is it actually using all 4 chips"*,
  *"why is the output wrong"*, *"is reasoning broken"*, *"is it actually
  batching"*.

Boundary: `tt:launch` owns everything before the server answers `/health`.
`tt:verify` does not restart or reconfigure the server — a fix routes back to
`tt:launch` (bad override) or `tt:run` (device-level recovery). `tt:retrieve`'s
captured spec entry is what V2/V9's "expected" values are checked against —
read it rather than assuming the served config matches what was requested.

## Resolve

Shell state does not survive between Bash calls. Start every call with:

```bash
SCRATCH=<session scratchpad>
EP=${ENDPOINT:-http://localhost:8000}
MODEL=<--model, or the model tt:launch reports running>
CID=<container id from tt:launch, needed for V7,V9,V14,V21,V22>
```

## Arguments

| Arg | Effect |
|---|---|
| `--endpoint <url>` | Server base URL. Default `http://localhost:8000`. |
| `--model <name>` | Expected served model. Enables V2, V9, V23. |
| `--questions V19,V20,V21` | Run only the debug-elimination subset, for a targeted bug report. |

## Pipeline

```
health → generation → hardware → performance → reasoning → debug (only on failure) → table
```

1. **Health & identity** (`health-and-generation.md` §1–2): V1, V2.
2. **Generation correctness** (`health-and-generation.md` §3): V3–V6.
3. **Hardware/mesh** (`hardware-and-performance.md` §1): V7–V9.
4. **Performance** (`hardware-and-performance.md` §2): V10–V12.
5. **Reasoning-model checks** (`reasoning-models.md`): V13–V18, only if the
   served model is a reasoning model.
6. **Debug/provenance** (`debug-and-provenance.md`): V19–V25, only invoked
   when an earlier step failed and needs a root cause.
7. **Table**: emit the output below.

Run each request one at a time; NEVER batch verification requests together —
a garbled response makes it impossible to tell which check produced it.

## Output

Fixed table, row order V1–V25 (rows N/A to this model, e.g. reasoning checks
on a non-reasoning model, are marked `n/a: not a reasoning model`, not
omitted), then a Sources footer.

**V26 — does the server survive an idle period?** is proposed, not yet
adopted in the dataset. Emit it and mark it `PROPOSED` in the ID cell until
the Sheet carries it; evidence is in `hardware-and-performance.md` § Status.

## Invariants

- All test requests are idempotent (`temperature: 0` where correctness
  matters) — NEVER a request that mutates server-side state.
- NEVER restart, reconfigure, or reset anything from this skill.
- A check that "answers" is not the same as a check that "passes" — always
  compare against the exact expected value/shape in the interpretation
  tables, not just HTTP 200.
- `debug-and-provenance.md`'s GitHub calls: ≤6 per run (1 search, ≤3 compare,
  1 issue-comments fetch), same budget as `tt:retrieve`. Unauthenticated
  limit is 60/h; a `GH_TOKEN` lifts it. NEVER echo a token used for auth.

## Red Flags

| Thought | Reality |
|---|---|
| "It answered, so it's healthy" | Check `/health`'s status code explicitly (V1) — a 200 during warmup can still be the wrong model (V2). |
| "Wrong output must be sampling" | Re-run at temperature 0 first (V19, `debug-and-provenance.md`) before touching anything else. |
| "reasoning_content is null, so this model must not support reasoning" | Read the engine config line for `reasoning_parser=` (V14) before concluding anything. There are three outcomes, not two: not wired, wired and working, and **wired and still null** — the last is what 0.20.0 does, with the think block generated and silently dropped (V13). |
| "the server answered /health an hour ago, so it is still up" | Not a safe assumption on this stack. On 2026-09-22 the first request after ~11 h idle took the engine down with a SIGBUS in `tt::umd::write32_to_device`. Re-check V1 before trusting any later row. |
| "16 parallel requests came back fast, so it's batching" | Only identical per-request latency across the parallel requests proves continuous batching (V11) — faster aggregate alone does not. |

## Progressive Load Table

| Sub-task | Load |
|---|---|
| Health, served model, raw/arithmetic/sequential/code generation checks | `health-and-generation.md` |
| Mesh membership, real work vs idle, KV-cache, throughput, batching, long prefill | `hardware-and-performance.md` |
| reasoning_content, parser metadata, undocumented flags, token budget, tool-call gate | `reasoning-models.md` |
| Root-cause elimination, quiet server warnings, known-bug search, fix-by-ancestry | `debug-and-provenance.md` |
| What the spec declares (reasoning_parser_name, max_context, known_issues) | invoke `tt:retrieve` |
| Idle telemetry baseline for V8's comparison | invoke `tt:discover --questions D12` |
| A launch-stage explanation is needed instead of a runtime one | hand back to `tt:launch` |
