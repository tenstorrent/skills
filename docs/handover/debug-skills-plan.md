# tt-debug-tools — Build Plan

One plugin in `tenstorrent/skills`. One skill per debugging question. Flat.

Each skill teaches an agent two things: how to drive its tool, and how to read
what comes back. Nothing else. No goals, no loops, no orchestration, no
research, no fix-and-verify cycles, no router.

Tool facts — env vars, commands, output shapes, traps, fixtures, existing
coverage elsewhere — are canonical in `debug-tool-support.md`. This file
owns **build state only**: where things land, the per-skill budget, in what order
they land, how they are proven, and what is still unknown.

Target repo: `tenstorrent/skills`. Strategy: that repo's issue #2.

## Scope boundary

In scope, per skill:

- The env vars, flags and CLI arguments the tool exposes.
- A runnable recipe that puts the stack in a state where the tool produces output.
- What the output looks like and what each field means.
- The traps: activations that silently do nothing, outputs that mislead.

Out of scope, everywhere:

- Deciding *which* tool to reach for. That is the agent's judgement, informed by
  each skill's `description`.
- Reproducing, isolating, classifying, fixing, verifying, or re-running. No phase
  tables, no exit conditions, no verdict formats, no stop conditions.
- Editing kernels, resetting boards as a recovery procedure, or any multi-step
  device hygiene sequence.
- Reading another skill's files, or invoking another skill.

A skill that starts growing an ordered procedure with a goal has left scope.

## Where it lands

```
plugins/tt-debug-tools/
  .claude-plugin/plugin.json      Claude manifest
  .codex-plugin/plugin.json       Codex manifest
  skills/
    tt-watcher/
      SKILL.md                    ≤120 lines target, 130 hard
      references/*.md             each <4500 bytes
      manifest.yaml               declared env vars, flags, paths, fixtures
    tt-dprint/
    tt-triage/
    ...
```

Flat. No buckets — those are a root-`skills/` concept and they track teams.

Repo-level additions, outside the plugin package:

| Path | Purpose |
|---|---|
| `.claude-plugin/marketplace.json` | add the plugin to the Claude catalogue |
| `.agents/plugins/marketplace.json` | add it to the Codex catalogue — both must list the same plugins |
| `.github/CODEOWNERS` | plugin owner |
| `scripts/extract_upstream_snapshot.py` | regenerates the Tier A snapshot |
| `tests/test_debug_tools_manifests.py` | Tier A |
| `tests/upstream_snapshot/` | vendored upstream name lists |

### Blocked on PR #3

`plugins/` and `scripts/` do not exist on `main`. Both arrive with
`tenstorrent/skills` PR #3, *Add opt-in plugin marketplace and skills finder*,
which is open. Until it merges this work waits or branches off
`yieldthought/skills-marketplace-foundation`. Do not build the scaffold
independently — it would conflict with the manifests, CODEOWNERS and
`validate.yml` that PR lands.

PR #3 also deletes `.agents/adr/`, moving its rules into a short root policy plus
a review-only rulebook scoped under `skills/`. Cite the rules, not the ADR paths.

### No overlap with `tt-autodebug`

The strategy plans a separate `tt-autodebug` plugin for `autodebug`, `autofix`
and later `autotriage`. Those are diagnose-and-fix loops. This plugin has none,
so the two do not compete: `tt-autodebug` owns methods, `tt-debug-tools` owns
tools. If `autotriage` wants triage mechanics it restates them — duplication is
accepted, and neither plugin may read the other's files.

## The merge test

One skill per tool was the starting rule. It produced clusters a developer would
have to choose between — four UMD binaries, two ways to read watcher data — and
if the choice is unclear to an agent it is unclear to a person. Same test, one
answer.

**Merge when a developer asking one question would have to pick between skills.
Split when the questions differ.**

| Question | Skills | Verdict |
|---|---|---|
| "inspect board and cluster state" | 4 × `tt-umd-*` | **merge** → `tt-umd-tools` |
| "read watcher data" | `tt-watcher`, `tt-watcher-dump` | **merge** → `tt-watcher` |
| "turn on assertion checking" | `tt-kernel-asserts`, `tt-llk-asserts`, `tt-llk-sanitizer` | **merge** → `tt-asserts` |
| "profile this" | `tt-tracy`, `tt-device-profiler` | **merge** → `tt-profiler` |
| "use TTNN's host-side debug modes" | `tt-graph-capture`, `tt-comparison-mode` | **merge** → `tt-ttnn-debug-modes` |
| "why is this kernel slow at cycle level" | `tt-perf-counters` | **split** — different question, own interpretation model, own hardware constraint |
| "run the triage suite" vs "read one address on one core" | `tt-triage`, `tt-exalens` | **split** — but each description must name the other as the boundary |

The three merges beyond the two you named follow the same logic:

- **Asserts.** Three env vars, one decision. And the load-bearing fact is that
  they interact: LLK asserts need lightweight asserts *or* watcher set as a
  reporter, or the run hangs with nothing reported. Split across three skills,
  a developer reads one and never learns they need a second.
- **Profiler.** The Tracy wrapper internally sets `TT_METAL_DEVICE_PROFILER=1`.
  They are one tool with two entry points, not two tools.
- **TTNN debug modes.** Same activation, same gotcha — silently inert unless
  fast runtime mode is off — and two adjacent answers: graph capture finds the
  hanging op, comparison mode finds the diverging one. Three lines of routing
  inside one skill.

`tt-exalens` is the next merge candidate if its `SKILL.md` lands thin. It stays
separate for now because it owns the JTAG and GDB paths and the
`--remote-exalens` pairing, which nothing else covers.

## The 17 skills

Names carry the `tt-` prefix and must be globally unique across the repo — pins
resolve by name, not path.

| Skill | Covers | Fixture |
|---|---|---|
| `tt-watcher` | watcher polling, in-kernel ring buffer, `PAUSE()`, debug delays, `watcher_dump` and the GDB dump | Yes — ~40 gtest cases |
| `tt-dprint` | `DEVICE_PRINT` / `DPRINT`, `TSLICE` | Yes — ~55 gtest cases |
| `tt-checkpoint` | `DEBUG_CHECKPOINT`, `debug_dump_cb`, `debug_dump_l1`, DST dumps, wall-clock timers | Yes — 6 gtest cases |
| `tt-noc-dump` | NoC debug dump | Yes — 1 named case |
| `tt-asserts` | lightweight kernel asserts, LLK asserts, LLK sanitizer, and how each is reported | Partial — LLK asserts have a documented recipe; sanitizer **MISSING** |
| `tt-exalens` | tt-exalens CLI, Python lib, `--server`, JTAG, GDB | **MISSING** |
| `tt-triage` | `tt-triage.py`, `tt-run-triage.py`, ~20 scripts | Partial — state easy to force, no golden report |
| `tt-inspector` | Inspector env surface, RPC, serialization, `generated/inspector/` | Yes — 4 gtest cases |
| `tt-operation-timeout` | host operation timeout + the timeout command hook | **MISSING** |
| `tt-dispatch-telemetry` | `dispatch_telemetry_dump` | **MISSING** |
| `tt-profiler` | Tracy capture, `profile_this.py`, `tracy-capture`, Device Program Profiler, `DeviceZoneScopedN`, NoC events, mid-run dump | Partial — a programming example exists |
| `tt-perf-counters` | hardware performance counters | **MISSING** |
| `tt-smi` | `tt-smi` list, status, reset flags | No fixture needed |
| `tt-umd-tools` | UMD `topology`, `telemetry`, `system_health`, `harvesting` | **MISSING** |
| `tt-ttnn-debug-modes` | TTNN graph capture, per-op golden comparison, op tracing | **MISSING** |
| `tt-npe` | tt-npe, plus generating the visualizer report | **MISSING** |
| `tt-ttsim` | simulator | **MISSING** |

Not built, with reasons, in `debug-tool-support.md`: `tt-topology` (flashes
firmware), `tt-toplike` (interactive TUI), `tt-flash` (sudo firmware update),
TT-NN Visualizer as a UI.

One consequence worth noting: merging the sanitizer into `tt-asserts` means
nothing starts life in `in-progress/` except `tt-dispatch-telemetry`. The two
verified assert families carry the skill and the sanitizer ships as a marked
`MISSING` section — better than holding a whole skill back.

### Dispatch still rests on descriptions

There is no router, so 17 `description` fields carry every dispatch decision.
That is the failure mode `tt_ops_code_gen/skills/debug-ttnn-op/DESIGN.md`
documents: correct content that never gets invoked. Merging reduced the surface
from 25 to 17 and removed the four worst-confusable clusters, which is most of
the fix. What remains:

1. A `description` is not a summary. It carries trigger phrases, symptom words,
   and the boundary against the nearest neighbour.
2. **Tier B stops being optional.** With no router, the dispatch eval is the only
   proof an agent can find the right skill.

## Budgets

Two rule sets apply. Where they disagree the target repo wins, because its tests
are what fail.

| Unit | `tt:skill-creator` `economy.md` | `tenstorrent/skills` | Binding |
|---|---|---|---|
| `SKILL.md` | target ≤120 lines, cap 180 | ≤130 lines, tested | **≤120 target, 130 hard** |
| Reference file | target ≤100 lines, cap 150 | <4500 bytes, tested | **<4500 bytes** — roughly 60–75 lines of prose, stricter than the line target |
| Single rule inside a reference | ≤15 lines, cap 25 | — | ≤15 lines |
| `CLAUDE.md`-style rulebook | — | 90 lines | n/a here |

4500 bytes is the constraint that shapes the work. A tool with a large flag
surface does not get one big reference; it gets several small ones split by
concern.

Estimated distribution, from content already verified:

| Skill | `SKILL.md` | References |
|---|---|---|
| `tt-watcher` | ~120 | 5 — feature flags, output shapes, state codes, debug delays, dump-without-watcher |
| `tt-asserts` | ~120 | 3 — one per assert family, each with its own reporting path |
| `tt-triage` | ~120 | 3 — script inventory, read order, signal→meaning |
| `tt-profiler` | ~115 | 3 — capture paths, device-zone instrumentation, CSV format |
| `tt-dprint` | ~110 | 2 — env surface and per-RISC macros, tile printing |
| `tt-umd-tools` | ~110 | 2 — build gate and invocation, per-binary output shapes |
| `tt-exalens` | ~100 | 2 — CLI and library, JTAG and GDB |
| `tt-checkpoint` | ~100 | 2 — knobs, output format and standalone dumps |
| `tt-perf-counters` | ~100 | 2 — capture paths and the bitfield, interpretation |
| `tt-ttnn-debug-modes` | ~90 | 1 — the two modes side by side |
| remaining 7 | ~60–90 | 0–1 |

Roughly 17 `SKILL.md` files and 25 references. Merging moved volume out of the
skill count and into references, which is the right direction: a reference is
read on demand, a skill description is read on every dispatch.

The merged skills are the ones at risk of blowing the 120-line target. If
`tt-watcher` or `tt-asserts` cannot route to its references inside 120 lines, the
merge was wrong and it splits back — that is the check, not a matter of taste.

## Distribution — what goes where

From `placement.md`, adapted to a plugin that cannot reach outside its package.

| Content | Goes in |
|---|---|
| Purpose, triggers, the env surface the skill owns, one canonical force-the-state recipe, where output lands, traps that change a decision | `SKILL.md` |
| Full flag tables, output-shape catalogues, field-meaning maps, per-arch divergence | `references/*.md` |
| Volatile API signatures | **Nowhere.** Name the source file; the agent reads it from the active tt-metal checkout. |

Three rules carried over and one adapted:

- **Never inline volatile content.** Point at source files, describe patterns and
  intent, not API signatures. tt-buddy resolves these through a research skill;
  this plugin has no such dependency, so a skill names
  `tt-metal/tt_metal/hw/inc/api/debug/device_print.h` and the agent opens it.
  Every skill here already assumes an active tt-metal checkout — that is what
  "force the state" runs against.
- **No source line numbers in skill content.** They rot every refactor. File
  basenames and stable identifiers — functions, registers, env vars, log
  literals, waypoint codes — are fine. The line ranges in § MISSING register are
  planning notes and must not travel into a skill.
- **Repo prefix on source paths.** `tt-metal/...`, `tt-umd/...`,
  `tt-exalens/...`. Paths inside the plugin stay unprefixed.
- **One canonical example per rule; tables beat prose; one sentence of *why* at
  most,** spent on warning against a tempting wrong move.

Within a skill, each rule lives in exactly one file and other mentions are
one-line pointers. Across skills the opposite holds: restate freely, because a
cross-skill pointer resolves to nothing when the skill is installed alone.

## Duplication is the design

Settled. gh-aw copies one skill folder and a plugin must not read outside its
package, so sharing breaks and duplication works.

- A skill restates whatever it needs.
- Cross-skill mentions are prose, not imports. A skill installed alone still works.
- A `references/` path may only point inside its own skill folder.
- Where two skills need an identical file, duplicate it and register the pair so
  a test asserts the copies stay byte-identical. Duplication is accepted; *drift*
  is the risk, and it gets enforced rather than trusted.

Registered duplicates expected at the start:

| Content | Copies in |
|---|---|
| Watcher / DPRINT / device-profiler share on-chip SRAM — one per run, silent corruption otherwise | `tt-watcher`, `tt-dprint`, `tt-profiler`, `tt-perf-counters`, `tt-noc-dump` |
| Watcher state codes | `tt-watcher`, `tt-triage` |
| A fired assert halts the core and looks like a hang, so it is read through triage | `tt-asserts`, `tt-triage` |

Merging cut this register down. `ebreak` semantics and the UMD build gate were
both going to be three- and four-way duplicates; each now lives once, inside the
skill that owns the decision.

No dependency on tt-buddy: no `tt:run`, `tt:note`, `tt:learn`, `tt:profiler`.

## Skill contract

Frontmatter, enforced by tests:

| Field | Value |
|---|---|
| `name` | equals the directory name, globally unique across the repo |
| `description` | trigger phrases, symptom words, and the boundary against the nearest neighbour |
| `metadata.tier` | one of `model \| op \| kernel \| process` |
| `metadata.upstream` | list of `{repo, ref, path}`, `ref` a 40-character lowercase SHA; `[]` for original work |

`metadata.upstream` is not bookkeeping — the drift audit parses it, and a
malformed entry silently drops that upstream. Every skill here is vendored;
nothing is original work.

`SKILL.md` body, one shape for every skill:

| Section | Contains |
|---|---|
| Purpose | Which tool, and what it answers. |
| When to invoke | Triggers and symptom words. The boundary against the nearest neighbour skill. |
| Surface | The env vars, flags and CLI arguments this tool exposes. |
| Force the state | One runnable recipe that makes the tool produce output. Names the gtest case where one exists. |
| Output | What lands, where, and what the fields mean. |
| Traps | Activations that silently do nothing; outputs that mislead. |

A section with nothing verified behind it says `MISSING` and names what would
fill it. An empty section is a bug; a marked one is a backlog item.

Two style rules from the target repo: skills never post (no `gh api -X POST`, no
`gh pr review`), and vendored prose is matched rather than restyled — em-dashes
stay, because restyling vendored text introduces transcription errors for no
gain.

## Drafts land in `in-progress/`

Already the repo's mechanism: `in-progress/` holds drafts, excluded from the
README and the plugin manifest. A skill whose MISSING markers outnumber its
verified sections goes there and is promoted when they clear. Keeps
`tt-dispatch-telemetry` — the only skill starting there — out of a user-facing
catalogue while still version-controlled and still linted.

## Eval architecture

The strategy sets the shape: fast manifest, reference and unit tests in the same
PR as the plugin; costly model or hardware evaluations in a **separate** PR, run
on a schedule or by manual request until the cost is understood.

### `manifest.yaml` — the declaration

```yaml
skill: tt-watcher
env_vars:
  - TT_METAL_WATCHER
  - TT_METAL_WATCHER_DISABLE_ASSERT
cli_flags: []
kernel_apis:
  - WATCHER_RING_BUFFER_PUSH
  - PAUSE
artifacts:
  - generated/watcher/watcher.log
fixtures:
  binary: unit_tests_debug_tools
  cases:
    - MeshWatcherFixture.TensixTestWatcherSanitize
output_literals:
  - "did not map to any known"
  - "tripped assert on line"
```

### Tier A — declaration join. Same PR. No device, no model, no API key.

`tests/test_debug_tools_manifests.py`. Per skill:

1. Every `env_vars` entry exists in the upstream env var list.
2. Every `fixtures.cases` entry exists in the upstream test sources.
3. Every `output_literals` entry exists in an upstream source or doc file.
4. **Reverse direction:** every `TT_METAL_*` token in the skill body appears in
   `manifest.yaml`. Catches under-declaration.
5. An env var declared by more than one skill is marked shared in both.
6. Registered duplicate references are byte-identical.
7. No `references/` path crosses a skill-folder boundary.
8. No line-number citations in skill content.

Same principle the repo already applies: an invariant earns a test when violating
it fails **silently**. A wrong env var name is exactly that — the skill reads
fine and the command does nothing. Checks 1–4 catch the error class already found
twice in prior art: `TT_METAL_DEVICE_PRINT=1`, which does not exist, and
`TT_METAL_WATCHER_DELAY`, which is an upstream doc's own prose error against its
own example.

**A separate test file is required.** `tests/test_skill_frontmatter.py` globs
`REPO/"skills"` only, so nothing under `plugins/` is covered today. Twenty-five
skills would otherwise land unlinted. The new file asserts the frontmatter
invariants and the size budgets for `plugins/tt-debug-tools/skills/**` as well as
the joins above.

**The snapshot problem.** CI has no tt-metal checkout, so upstream lists are
vendored:

```
tests/upstream_snapshot/
  env_vars.txt        from tt-metal/tt_metal/llrt/rtoptions.cpp
  gtest_cases.txt     from tt-metal/tests/tt_metal/tt_metal/debug_tools/
  doc_literals.txt    from tt-metal/docs/source/tt-metalium/tools/*.rst
  COMMIT              the tt-metal commit these came from
```

`scripts/extract_upstream_snapshot.py` regenerates them; a scheduled workflow
opens a PR when upstream drifts. Per-PR runs stay offline and deterministic, and
drift arrives as its own reviewable PR instead of a red build on an unrelated
change. `COMMIT` must match the `metadata.upstream` refs, or the snapshot
validates against a different tree than the skills were vendored from.

Stdlib only. The `meta/` pyyaml exemption is scoped to root `skills/` maintenance
tooling and does not extend here.

### Tier B — dispatch and command shape. Separate PR. Model, no device.

Does the right skill fire from a symptom prompt, and does it emit a runnable
command with the correct env vars, flags and output paths? Measure invocation,
not answer quality.

**With no router this is the eval that matters most.** One prompt per skill,
plus the pairs that survive the merge test and can still mis-fire:
`tt-triage` / `tt-exalens`, `tt-profiler` / `tt-perf-counters`,
`tt-dprint` / `tt-checkpoint` (both print from the kernel), and
`tt-asserts` / `tt-watcher` (watcher is one of the assert reporters).

Gated on an approved CI model and a cost limit — the strategy requires both
before scheduled runs. Plugin validators run per PR regardless:
`claude plugin validate plugins/tt-debug-tools --strict`, plus the Codex plugin
and skill validators.

### Tier C — interpretation. Separate PR. Needs TT hardware.

Feed a real artifact to a skill and check that it reads the fields correctly.
Fixtures are the upstream gtest cases, which already force the state and assert
golden strings.

**MISSING** — no hardware runner. Adopt the CI patterns the strategy names: build
the matrix from discovered inputs, keep independent cases running after one
failure, set time limits, upload small diagnostic artifacts for failed runs.

### CI wiring

| Workflow | Runs | Status |
|---|---|---|
| `validate.yml` (arrives with PR #3) | Tier A + plugin validators, every PR | extend |
| `upstream-drift.yml` | snapshot re-extract, scheduled | new |
| `debug-tools-evals.yml` | Tier B, scheduled or manual | new, separate PR |
| Tier C | scheduled, self-hosted | blocked on a hardware runner |

## Build order

Twenty-five skills do not land in one PR. The plugin lands once with a proving
set; the rest are follow-ups to a validated package.

| # | Step | Exit criterion |
|---|---|---|
| 0 | PR #3 merges | `plugins/` and `scripts/` exist on `main` |
| 1 | Plugin scaffold + `tt-triage` + `tt-noc-dump` | Dual manifests, both catalogues, CODEOWNERS. Validators pass. Clean install on Codex and Claude. Most and least complex skill, both inside budget. |
| 2 | Tier A harness, same PR | Both manifests pass all 8 checks. A deliberately broken env var name fails the build. |
| 3 | `tt-watcher` + `tt-dprint` | Highest traffic; `tt-dprint` is the largest gap in existing coverage. `tt-watcher` proves a 5-reference split fits 4500 bytes each and still routes inside 120 lines. |
| 4 | `tt-asserts` + `tt-exalens` | `tt-asserts` proves a three-family merge routes cleanly, with the reporter dependency as its central trap. |
| 5 | `tt-checkpoint`, `tt-inspector`, `tt-operation-timeout` | All pass Tier A. |
| 6 | `tt-profiler`, `tt-perf-counters` | All pass Tier A. The shared-SRAM duplicate registered across five skills. |
| 7 | `tt-smi`, `tt-umd-tools` | Build gate stated once, inside `tt-umd-tools`. |
| 8 | `tt-ttnn-debug-modes`, `tt-npe`, `tt-ttsim` | All pass Tier A. |
| 9 | `tt-dispatch-telemetry` → `in-progress/` | Linted, not promoted, MISSING markers intact. |
| 10 | Finder catalogue entry | `tt-skills-finder` recommends the plugin for debug tasks and does not install it without user action. |
| 11 | Tier B PR | Every skill fires from its symptom prompt; the four surviving near-neighbour pairs do not cross-fire. |
| 12 | Tier C PR, when a hardware runner exists | One skill reads a real artifact correctly. |

Step 1 pairs the most and least complex skill deliberately. If one body shape
cannot hold both `tt-triage` and `tt-noc-dump` inside 120 lines, fix the contract
before writing 15 more against it. Steps 3 and 4 then test the merges: a merged
skill that cannot route inside budget splits back.

## MISSING register

### No fixture upstream

`tt-exalens`, `tt-dispatch-telemetry`, `tt-ttnn-debug-modes`, `tt-npe`,
`tt-ttsim`, `tt-perf-counters`, `tt-operation-timeout`, `tt-umd-tools`, and the
sanitizer third of `tt-asserts`.

Built and passing Tier A on their declarations; Tier C cannot cover them until a
fixture is authored. Build anyway — a verified surface with no interpretation
test still beats no skill.

### Source not yet read

Line ranges are planning notes. They must not travel into skill content.

| Skill | Not read |
|---|---|
| `tt-dispatch-telemetry` | `tt-metal/tt_metal/tools/dispatch_telemetry_dump/`. No doc page. Output shape unknown. |
| `tt-asserts` | `tt-metal/tt_metal/tt-llk/common/sanitizer/output.h` — no doc page, severity semantics unverified. `llk_asserts.rst` lines 141–324. |
| `tt-perf-counters` | `perf_counters.hpp`, `tools/tracy/perf_counter_analysis.py`. Model is from ClaudeCurriculum, not upstream. |
| `tt-profiler` | `tracy_profiler.rst` apart from the capture section; `device_program_profiler.rst` lines 32–80, 145–166. |
| `tt-checkpoint` | `checkpoint.rst` lines 40–359, 395–505. |
| `tt-dprint` | `device_print.rst` lines 78–199. |
| `tt-watcher` | `watcher.rst` lines 1–115; `tt-metal/tt_metal/tools/watcher_dump/` entirely. |
| `tt-triage` | `tools/triage/tt-triage.md` body; the ~20 script sources. |
| `tt-noc-dump` | The `unit_tests_noc_debugging` source. Output taken from the doc's claim. |
| `tt-umd-tools` | `system_health` and `harvesting` output examples — only `--help` documented. |
| `tt-operation-timeout` | Nothing beyond the env var names and the LLK-assert worked example. |
| `tt-ttsim` | `TT_METAL_SIMULATOR` value format. |
| `tt-ttnn-debug-modes` | The exact `TTNN_CONFIG_OVERRIDES` JSON keys; the `ttnn.graph` API in ttnn source. |

### Unresolved

| Question | Blocks |
|---|---|
| PR #3 merge date; branch off it or wait | step 1 |
| `metadata.tier`'s four values are review-oriented. Which fits a device tool — `process`, or does the enum need a value? | step 1 frontmatter |
| Approved CI model and cost limit for Tier B | step 10 |
| No golden triage report upstream; a Tier C triage test asserts on section presence and named fields, not whole-file equality | `tt-triage` Tier C |
| Does `tt-exalens` stay separate, or fold into `tt-triage`? Keep it while it owns JTAG, GDB and `--remote-exalens`; fold if its `SKILL.md` lands thin | step 4 |

## Vendoring obligations

Every skill is vendored, two source repos are private, the target repo is public.
Before any skill lands:

- Strip internal-only pointers, machine-specific paths, personal identity
  mappings. Architecture detail stays, Quasar included — do not re-expand that
  scope by instinct.
- Record every source in `metadata.upstream` with a pinned SHA.
- Credit every newly vendored repo in `README.md` and regenerate `SOURCES.md`
  with the drift audit's `--sources` mode. An uncredited source is the failure
  the attribution test exists to catch.
- Vendoring is not endorsement. `debug-tool-support.md` records upstream
  errors found by checking rules against the code they describe; keep doing that
  rather than trusting provenance.
