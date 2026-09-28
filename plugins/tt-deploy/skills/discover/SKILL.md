---
name: discover
description: "Answer the 16 Discovery-stage host questions deterministically — host facts, driver, boards, hugepages, docker, HF cache, port 8000, device holders, --tt-device string, firmware minimums, idle telemetry, reset verdict, deployable models, gating, disk, clean-box. Fixed commands, fixed docs, fixed output shape. Read-only. Use before any inference deploy on a TT host."
metadata:
  layer: tool
---

# TT Discover

## Purpose

Produce the Discovery table for the machine the agent is running on. Every
answer comes from a listed command or a listed upstream doc, interpreted by
a listed rule. No memory, no inference from product pages, no sampling.

**Read-only.** NEVER reset, kill, pull, download, `docker rm`, or write
outside the scratchpad. The reset question returns a verdict; the reset
itself is `tt:run`'s job.

## When to Invoke

Invoke `tt:discover` (Skill tool) when:

- The user asks any Discovery question: *"what's on this machine"*, *"which
  boards / driver / firmware"*, *"can I deploy X here"*, *"what do I pass to
  --tt-device"*, *"is port 8000 free"*, *"is anything holding the device"*,
  *"do I need a reset"*, *"is this box clean"*.
- A deploy or launch task starts and no Discovery table exists for this session.

Boundary: `tt:run` workspace detection answers *"can I run a job from this
cwd"*. `tt:discover` answers *"what is this host and what can it deploy"*.

## Arguments

| Arg | Effect |
|---|---|
| none | Answer D1–D16. D7 and D15 return `requires --model`. |
| `--model <org>/<name>` | Enables D7, D15; scopes D6 and D11 to that model's spec entry. |
| `--questions D3,D9` | Answer only the listed IDs. Dependencies (below) still run. |
| `--note` | Also record the table via `tt:note`, topic `discover-<hostname>`. Default: chat only. |

## Pipeline

```
snapshot → host → boards → models → table
```

1. **Snapshot** (always, when `/dev/tenstorrent/` has ≥1 entry): capture
   the tt-smi JSON once per `boards.md` § Capture. Every board question
   reads the file. NEVER re-run tt-smi per question.
2. **Host**: `host.md` — D1, D2, D4, D5, D8, D13.
3. **Boards**: `boards.md` — D3, D9, D10, D12, D14. Sets `DEV`.
4. **Models**: `models.md` — D6, D7, D15, D16. Fetches upstream docs.
5. **D11**: `boards.md` § Interpretation, using step 4's spec output.
6. **Table**: emit the output below. Nothing else.

Run each file's commands in one Bash call per file. Three Bash calls plus
the network fetches is the budget.

## Output

One table, fixed row order D1–D16, then a one-line Sources footer.

```
| ID | Question | Answer | Evidence |
|---|---|---|---|
| D1 | Host facts | <cores>, <RAM>, <disk free>, <distro>, <CPU> | nproc; /proc/meminfo; df; os-release; lscpu |
...
Sources: tt-inference-server@main <short-sha> fetched <ISO time>; tt-smi <ver>
```

Answer cells use the exact format each question specifies in its sub-file.
Comparisons (D11) state both sides and the verdict. Unknowns say `unknown:
<why>`, never a guess. A failed network fetch says `fetch failed: <url>`.

## Invariants

- No `/dev/tenstorrent/`: answer D3, D9–D14 as `no device` and continue.
- `tt-smi` MUST be located per `boards.md` § Capture. NEVER assume it is on PATH.
- Upstream docs are fetched from GitHub raw at `main` every run. NEVER read
  a local tt-inference-server checkout; NEVER cache between sessions.
- Spec-key vs doc-suffix casing: see `knowledge/hardware/boards.md` § Doc naming.

## Red Flags

| Thought | Reality |
|---|---|
| "The getting-started page says p150x4 for this box" | Derive D9 from board serials. Docs describe SKUs, not this host. |
| "HugePages_Total in /proc/meminfo is 0, so no hugepages" | That line is the default page size only. Read the sysfs per-size pool. |
| "I'll run tt-smi -r since it needs a reset" | NEVER. Report the verdict. Reset is `tt:run` recovery. |
| "I'll skip the snapshot and eyeball tt-smi -ls" | `-ls` truncates serials. The snapshot is the only complete source. |
| "I'll answer gating from memory" | One curl per model. 200 with `gated` field or nothing. |
| "The local checkout is newer, I'll read that" | Raw at main, every time. Drift is reported, not hidden. |

## Progressive Load Table

| Sub-task | Load |
|---|---|
| Host, driver, hugepages, docker, HF cache, port, device holders | `host.md` |
| Snapshot capture, boards vs ASICs, device string, firmware, telemetry, reset verdict | `boards.md` |
| Deployable models, gating, disk budget, clean-box | `models.md` |
| Board type → ASIC count → device string | `knowledge/hardware/boards.md` |
| Recording the table | invoke `tt:note` |
| Acting on a reset verdict | invoke `tt:run` |
