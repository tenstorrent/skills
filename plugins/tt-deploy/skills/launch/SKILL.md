---
name: launch
description: "Diagnose an in-progress or completed model launch on Tenstorrent hardware — launch steps, mandatory docker flags, failure-signature classification, minimum-override selection and safety, override persistence, realistic timing, and displaced services. Deterministic, evidence-based. Use once a launch attempt has started (docker run / run.py already invoked)."
metadata:
  layer: tool
---

# TT Launch

## Purpose

Diagnose a model launch that is running, hanging, or has failed, and produce
the Launch table (L1–L9) plus a ranked fix when something is wrong. Every
answer comes from a captured log/state snapshot or a listed static rule.

## When to Invoke

Invoke `tt:launch` (Skill tool) when:

- `docker run` / `run.py` has already been issued for a model launch.
- The user asks: *"is this hanging"*, *"why did it fail"*, *"what flags do I
  need"*, *"how long should this take"*, *"did I break something else on this
  box"*, *"is my override safe"*.

Boundary: `tt:discover` answers what the machine *is*, before a launch starts.
`tt:retrieve` answers what the model/image *should be* — its spec entry,
docker image tag, mandatory flags, gating. `tt:launch` answers what a launch
*is doing*, once one is running, against what `tt:retrieve` said it should
be. `tt:verify` takes over the moment the server actually answers `/health`.

## Resolve

Shell state does not survive between Bash calls. Start every call with:

```bash
SCRATCH=<session scratchpad>
CID=${CONTAINER:-$(docker ps -a --format '{{.CreatedAt}}\t{{.ID}}\t{{.Image}}' | grep -E 'vllm-tt-metal|tt-inference-server' | sort -r | head -1 | cut -f2)}
OVERRIDE=<--override, if any>
echo "container=${CID:-NONE}"
```

If `tt:retrieve` was run first for this model, its captured entry
(`$SCRATCH/entry_*.json`) is the source of truth for what flags/image *should*
be running — read it before assuming a launch command was correct, rather
than re-deriving from documentation.

## Arguments

| Arg | Effect |
|---|---|
| `--container <id>` | The launching container. Required for L1, L4, L6, L7, L9. Omit → read the most recently created container matching a `vllm-tt-metal` / `tt-inference-server` image. |
| `--override <path>` | Path to a spec override file, if one is in use. Enables L6, L7. |

## Pipeline

```
capture → classify → table
```

1. **Capture** (`diagnose.md` § Capture): one `docker inspect` + `docker logs`
   snapshot, cached to `$SCRATCH/launch-log.txt`. NEVER re-tail logs per
   question.
2. **Classify** (`diagnose.md` § Interpretation): answer L1, L2, L4, L8, L9,
   L10 from the captured log/state.
3. **Command & overrides** (`override.md`): answer L3, L5, L6, L7 — only
   loaded when the question actually concerns the launch command itself, not
   for every diagnosis.
4. **Table**: emit the output below. Nothing else.

## Output

Fixed table, row order L1–L10, then a one-line Sources footer. L10 (silent
fallbacks and env overrides) is proposed, not yet adopted in the dataset —
emit it, and mark it `PROPOSED` in the ID cell until the Sheet carries it.

```
| ID | Question | Answer | Evidence |
|---|---|---|---|
...
Sources: docker logs <container> captured <ISO time>; static launch knowledge
```

## Invariants

- Read-only against the device and the container. NEVER `docker kill`,
  `docker rm`, `docker restart`, or `tt-smi -r` here — a fix decision routes
  to `tt:run` recovery. This skill only diagnoses.
- NEVER guess a failure class from partial output. If none of the known
  signatures match, report `unknown failure: <last 20 log lines>`, never a
  guess — that is itself a new signature to add here, not a dead end.
- NEVER print the value of `HF_TOKEN` or `JWT_SECRET`, even when they appear
  in a captured log or `.env` file. Report `set`/`unset` only.

## Red Flags

| Thought | Reality |
|---|---|
| "It's been quiet for a minute, must be hanging" | Kernel JIT/tilize is silently long-running. Compare elapsed time against the step list in `diagnose.md` § L2, not silence alone. |
| "The published '10–20 min' first-run estimate says this is late" | NEVER cite a single range as ground truth — not the doc's, and not the "8–22 min cold / 6–8 warm" figure this file used to give, which our own run refuted: 29 min with a *warm* 29 GB tensor cache, mesh assembly alone 10 m 48 s. "Warm" is not one variable — the weights cache and the tensor cache warm independently. Cite `diagnose.md` § Stage markers, phase by phase. |
| "I'll bind-mount the whole repo spec, it's simpler" | Overrides are ranked for a reason. Try the surgical merge first; state why before reaching for `--dev-mode`. |
| "The override file looks right, ship it" | NEVER skip the byte-diff against upstream (L6) — a hand-edited override can silently pin the wrong image while looking correct. |

## Progressive Load Table

| Sub-task | Load |
|---|---|
| Is it hanging, what step, what failure, timing, what got displaced, silent fallbacks (L1, L2, L4, L8, L9, L10) | `diagnose.md` |
| Mandatory flags, override selection and safety (L3, L5, L6, L7) | `override.md` |
| What the image/spec *should* look like (docker image tag, mandatory flags, gating) | invoke `tt:retrieve` |
| Acting on a failure (reset, restart) | invoke `tt:run` |
| Server finally answers `/health` | hand off to `tt:verify` |

## Note on shared knowledge

`knowledge/recipes/tt-inference-server/container.md` now exists and carries
the mandatory docker flags, the baked-spec extraction, the run.py-vs-docker
rule and the `has_builtin_warmup` branch — all verified 2026-09-22 against
the live container. Where it and the inline copies in `diagnose.md` /
`override.md` disagree, **the shared file is the current one**.

The duplication is deliberate for now, not resolved: `launch` is still
usable standalone, but L2/L3/L8's static facts exist in two places and only
one of them is maintained. Collapsing them is a follow-up, not a decision
this file makes.
