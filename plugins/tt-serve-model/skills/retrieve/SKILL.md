---
name: retrieve
description: "Answer the Model Retrieval-stage questions deterministically for one model on this host — HF existence, gating, config; tt-inference-server spec entry (image, commits, status, ceilings, known_issues); image-vs-repo spec drift; GHCR tag, size, ancestry; run.py vs docker; and the tt-model-manager bundle path (search, catalog, arch, manifest, weights pointer, install state, exact serve command). Fixed commands, fixed docs, fixed output shape. Read-only unless --pull. Use before pulling or launching any model on a TT host."
metadata:
  layer: tool
---

# TT Retrieve

## Purpose

Produce the Model Retrieval table for one model on this machine, from both
ecosystems: tt-inference-server (release spec + GHCR images) and
tt-model-manager (community bundles on the HF Hub). `tt` (tt-cli) fronts
both; use it when present and the same APIs it reads when not. Every answer
comes from a listed command or upstream file. No memory, no plausible names.

**Read-only by default.** NEVER download, install, reset, or launch; `--pull`
alone runs `pull.md`.

## When to Invoke

Invoke `tt:retrieve` (Skill tool) for any model question before deployment:
*"does X exist / is it gated"*, *"is there a TT implementation for my board"*,
*"what image, commits, limits"*, *"is the image's spec stale"*, *"is there a
bundle for X"*, *"what will tt-model serve run"*, *"how do I pull X"* — or
when a deploy task has its Discovery table and needs the model side.

Boundary: `tt:discover` answers *what is this host*. `tt:retrieve` answers
*what is this model and how does it get here*. Launching is not here.

## Arguments

| Arg | Effect |
|---|---|
| `--model <id>` | HF repo id, spec `model_name`, or bundle id. Required for all rows but R1, B1–B3. |
| `--search <term>` | R1, B1–B3: list candidates. |
| `--device <KEY>` | Spec key (`P150`, `P300X2`). Default: invoke `tt:discover --questions D9`. |
| `--fix-sha <sha>` | Enables the R15 ancestry verdict. |
| `--questions R6,B7` | Answer only the listed IDs. Dependencies still run. |
| `--pull` | After the table, execute `pull.md`. Default: never. |
| `--note` | Also record via `tt:note`, topic `retrieve-<model>`. Default: chat only. |

## Pipeline

`resolve → hf → spec → image → bundle → table [→ pull]`

1. **Resolve** (below): tools, auth headers, `DEV`, which paths apply.
2. `hf.md` — R1–R4, R8. 3. `spec.md` — R5, R6, R9–R12, R16.
4. `image.md` — R7, R13–R15; `n/a` when R5 is `no`. 5. `bundle.md` — B1–B20; `n/a` when no bundle matches.
6. **Table**, then `pull.md` only under `--pull`.

One Bash call per sub-file. Five calls plus the network fetches is the budget.

## Resolve

Shell state does not survive between Bash calls. Start every call with:

```bash
SCRATCH=<session scratchpad>; MODEL=<--model>; DEV=<--device or D9, uppercase>; FIX=<--fix-sha>; SEARCH=<--search>
ghapi(){ curl -s ${GH_TOKEN:+-H "Authorization: Bearer $GH_TOKEN"} "$@"; }
TT=$(command -v tt); TM=$(command -v tt-model || ls ${TT_DATA_DIR:-~/.local/share/tenstorrent}/bin/tt-model 2>/dev/null)
echo "tt=${TT:-absent} tt-model=${TM:-absent}"
```

`hfapi` and the HF token come from `knowledge/hf-hub.md` § Inspect one repo,
which every HF-touching call runs first.

Paths: R applies when the spec has an entry for `$MODEL`. B applies when the
repo carries `tt_kernel_manifest.json` (R8 `bundle_manifest`) or the id is in
the local install index. Both may apply; run both. Neither: R1–R4, R8, R5
`no`, and B1–B3 with `--search ${MODEL##*/}`.

## Output

One `| ID | Question | Answer | Evidence |` table, fixed row order R1–R16
then B1–B20, then one footer line: `Sources: tt-inference-server@main <sha>
fetched <ISO>; HF API <ISO>; GHCR <ISO>; tt <ver|absent> (catalog release
<ver>); tt-model <ver|absent>`.

Answer cells use each sub-file's exact format. Skipped-path rows read `n/a:
<why>`; unknowns `unknown: <why>`, never a guess; failed fetches `fetch
failed: <url>`. Token values are never printed.

## Invariants

- Spec and docs come from GitHub raw at `main` every run; NEVER a local
  checkout or `tt`'s bundled catalog (report its pin in Sources only).
- Existence is an HTTP 200 from the HF model API with a file count.
- GitHub API: ≤6 calls per run (fetch 1, R13 1, R15 ≤3). Unauthenticated
  limit is 60/h; `GH_TOKEN` lifts it.
- Ancestry comes from the compare API only. Version strings never decide it.
- `docker create`/`cp` (R7) is the only container action. NEVER `docker run`.
- Authenticated fetches go through `hfapi`/`ghapi`. NEVER expand a header
  string from a variable; NEVER echo the token.

## Red Flags

| Thought | Reality |
|---|---|
| "The per-model doc has the docker run, done" / "I know this family is gated" | R7 reads the image's baked spec; doc and image drift by a release. Gating is one curl, never memory. |
| "`tt model info` is faster than the spec" | It is the cross-check. Its catalog pins an older release than `main`. |
| "The newer tag is numerically higher, so it has the fix" | Compare API on tt-metal commits, or `requires --fix-sha`. |
| "Bundle says 4 chips, I have 4 chips" | Boards ≠ chips. `P300X2` and `P150X4` are both 4 chips; report `mismatch`. |
| "I'll pull the image for R7" / "run tt-model serve to see the command" | `not local` and `--print --local-only`. Pulling is `--pull`'s job; serving is a launch. |

## Progressive Load Table

| Sub-task | Load |
|---|---|
| Existence, gating, config.json, architecture, weight size | `hf.md` |
| HF API calls, `hfapi`, token state, status-code traps | `knowledge/hf-hub.md` |
| Spec entry, exact implementation, ceilings, known_issues, run.py vs docker | `spec.md` |
| Image spec drift, dates, GHCR tag/size, newer tags, ancestry | `image.md` |
| Bundles: search, catalog, manifest, weights, install, serve command | `bundle.md` |
| Executing the retrieval (`--pull`) | `pull.md` |
| Spec fetch, layout, queries, per-model doc path | `knowledge/recipes/tt-inference-server/model-spec.md` |
| Image naming, mandatory flags, baked spec, run.py token rule | `knowledge/recipes/tt-inference-server/container.md` |
| Device key, board count for this host; boards vs ASICs per spec key | invoke `tt:discover --questions D9,D10`; `knowledge/hardware/boards.md` |
| Recording the table | invoke `tt:note` |
