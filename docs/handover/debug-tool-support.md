# Debug Tool Support Matrix

Tracks which Tenstorrent debug tools have agent-skill coverage, and where. One
row per tool. Goal: a skill per tool in the `tt-debug-tools` plugin of
`tenstorrent/skills`, plus evals proving each skill drives its tool correctly.

Build state — landing paths, contract, eval tiers, order, open gaps — lives in
`debug-skills-plan.md`. This file owns the tool facts.

`tt-debug-tools` is flat: 17 skills, one per debugging question. Each teaches an
agent to drive its tools and read the output. No router, no workflows, no loops.
Where several tools answer one question they share a skill — the merge rule and
the resulting set are in the plan.

Every pointer in this document was verified against the current `main` of the
named repo on 2026-08-31 (via GitHub contents/code API). Rows marked
**unread** mean the pointer exists but its content has not been read yet —
read it before writing the skill; do not write from memory.

## Status vocabulary

| Status | Meaning |
|---|---|
| `none` | No existing skill mentions the tool. |
| `incidental` | Named only as a prerequisite or trap inside another tool's flow. No enable/run/interpret guidance. |
| `partial` | Some mechanics captured; the tool is not driven end-to-end. |
| `covered` | Enable → run → interpret all owned by a skill, with traps documented. |

The `Owner` column names the planned skill in `plugins/tt-debug-tools/skills/`.
Eval column is `—` for every row: no debug evals exist yet.

## How these are grouped

Tools are grouped by **when you have to decide to use them**, not by where the
code runs. When a job hangs, that is the question that decides what you can
still do: you either turned the tool on before launching, or you didn't.

- **Family 1 — turn on before the run.** Compiled into the kernel, or polled by
  a thread inside the test process. Slows the run down and changes its timing.
  The three biggest ones share the same on-chip memory, so you can only use one
  at a time.
- **Family 2 — attach to a run that is already stuck.** Run from a second shell
  against the hung process. Nothing to compile in, nothing to decide ahead of
  time. These read the **device**: RISC callstacks out of mailboxes, NoC
  registers, ARC telemetry, the code actually loaded on the cores.
- **Family 3 — read host state.** What the Metal runtime on the host thinks is
  happening: which program, which op, which kernel binary, what the tensors are.

The split is not perfectly clean, and pretending it is would mislead:

- Triage is not truly zero-setup. Its dispatcher-aware scripts need Inspector to
  have been running (it is on by default, so this is usually free), and
  `TT_METAL_INSPECTOR_SERIALIZE_ON_DISPATCH_TIMEOUT` really is setup you do
  beforehand. Triage belongs in family 2 because you can *reach for it after the
  fact*, not because it needs nothing.
- Triage halts cores to read them, so it changes device state too. That is why
  `check_broken_components` reports noise.
- Families 1 and 2 are **chained, not parallel.** A failed assert halts the core
  with an `ebreak`, which from outside looks exactly like a hang — so you read
  the assert *through* triage. Turning on an assert without knowing how to read
  it back wastes the run. Two skills, and each has to point at the other.

---

## Family 1 — Turn on before the run

| Tool | Verified source of truth | Activation | Existing coverage | Owner | Eval |
|---|---|---|---|---|---|
| **watcher** | `tt_metal/impl/debug/watcher_server.cpp`, `watcher_device_reader.cpp`; `docs/source/tt-metalium/tools/watcher.rst` (**unread**); `tt_metal/tools/watcher_dump/` (**unread**) | `TT_METAL_WATCHER=<N>` + 11 `TT_METAL_WATCHER_DISABLE_*` flags | `partial` — `skills/debugger/watcher.md` covers enable, flags, log read. Missing: debug delays, in-kernel features, `_NOINLINE`, `_TEXT_START`, `_TEST_MODE`, `_ENABLE_NOC_SANITIZE_LINKED_TRANSACTION`. Reading watcher data off a dead process is tracked separately in family 2 | `tt-watcher` | — |
| **DEVICE_PRINT / DPRINT** | `tt_metal/hw/inc/api/debug/device_print.h`, `api/debug/dprint.h`; `tech_reports/Debugging/DEVICE_PRINT_replaces_DPRINT.md` (**unread**); `docs/source/tt-metalium/tools/device_print.rst` (**unread**) | `TT_METAL_DPRINT_CORES` / `_RISCVS` / `_CHIPS` / `_NODES` / `_MESH_COORDS` / `_ETH_CORES` / `_DRAM_CORES` / `_DISPATCH_CORES` / `_FILE` / `_ONE_FILE_PER_RISC` / `_PREPEND_DEVICE_CORE_RISC` | **`none`** — largest gap. Zero mentions in `skills/debugger/`. `skills/profiler/` names `TT_METAL_DPRINT_CORES` only as a profiler conflict | `tt-dprint` | — |
| **Debug checkpoints** | `docs/source/tt-metalium/tools/checkpoint.rst` (**unread**) | `TT_METAL_CHECKPOINT` | `none` | `tt-checkpoint` | — |
| **NoC debug dump** | `tt_metal/impl/debug/noc_debugging.cpp`, `noc_logging.cpp`; `docs/source/tt-metalium/tools/noc_debug_dump.rst` (**unread**) | `TT_METAL_NOC_DEBUG_DUMP`, `TT_METAL_RECORD_NOC_TRANSFER_DATA` | `none` | `tt-noc-dump` | — |
| **Lightweight kernel asserts** | `docs/source/tt-metalium/tools/lightweight_kernel_asserts.rst` (**unread**); triage script `dump_lightweight_asserts` | `TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS` | `partial` — `skills/debugger/scripts.md` reads the triage script's output; no enablement guidance, so the skill can read asserts it never turned on | `tt-asserts` | — |
| **LLK asserts** | `docs/source/tt-metalium/tools/llk_asserts.rst` (**unread**); `tools/setup_llk_assert_env.sh` (read) | `TT_METAL_LLK_ASSERTS=1`; env setup via `source tools/setup_llk_assert_env.sh <assert_out> <dprint_out>` | `none` | `tt-asserts` | — |
| **LLK sanitizer** | `tt_metal/llrt/rtoptions.cpp`; `tt_metal/tt-llk/common/sanitizer/output.h`; build emits `-DLLK_SAN_ENABLE`. **No docs page** — upstream doc gap | `TT_METAL_LLK_SANITIZER` + severity `_PEDANTIC` / `_WARN` / `_ERROR` / `_FAULT` / `_INFO` / `_INTERNAL` | `none` | `tt-asserts` | — |

Notes:

- **`DEVICE_PRINT` vs `DPRINT`.** `dprint.h` states DPRINT is a thin alias for
  DEVICE_PRINT, and a tech report is titled `DEVICE_PRINT_replaces_DPRINT.md` —
  yet `docs/source/tt-metalium/tools/index.rst` still says "``DPRINT(...)`` is
  the recommended user-facing macro". Resolve from the header + tech report,
  not the index.
- **`TT_METAL_DEVICE_PRINT=1` is not real.** The prior-art agent doc
  (`tt_ops_code_gen/agents/ttnn-expert-debugger.md`) prescribes it. It is not in
  the `EnvVarID` list in `rtoptions.cpp`; only `TT_METAL_DEVICE_PRINT_DISPATCH_*`
  vars exist. The feature is named `DPRINT` and is enabled by the
  `TT_METAL_DPRINT_CORES` family. Do not copy that env var into a skill.
- **Watcher flag-name discrepancy.** `rtoptions.cpp` contains both
  `TT_METAL_WATCHER_DISABLE_NOC_SANITIZE` and
  `TT_METAL_WATCHER_DISABLE_SANITIZE_NOC`. `watcher.md` documents only the
  latter. Confirm which is live before writing `tt-watcher`.
- **Assert families all halt via `ebreak`.** From the outside a fired assert is
  indistinguishable from a hang: dispatch times out, triage runs. Any skill in
  this family must state that the hang *is* the assert.
- **`TT_METAL_LLK_ASSERTS=1` alone is a trap.** It needs either
  `TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS=1` or `TT_METAL_WATCHER` set for failure
  *reporting*; on its own it produces a hang with no diagnosis path. This is the
  single most important fact for `tt-llk-asserts`, and it is why
  `tools/setup_llk_assert_env.sh` pairs the two. ClaudeCurriculum enforces it
  with a `llk_asserts_without_reporter` warn hook.
- **Watcher has in-kernel features tt-buddy does not document:**
  `WATCHER_RING_BUFFER_PUSH(uint32_t)` — 31-element per-RISC ring buffer, **no
  cross-RISC sync** (multi-RISC use on one core is UB) — and `PAUSE()`, which
  halts the kernel until ENTER on the host CLI. Triage reads the ring buffer via
  `dump_watcher_ringbuffer.py`.
- **Watcher state can be dumped from GDB without watcher enabled:**
  `call tt::watcher::dump(stderr, true)` from a frame in the `tt::` namespace.
  Post-mortem path for a host-side assert or segfault. Debug-only state
  (waypoints) is absent, the rest dumps.
- **Race-repro debug delays.** `TT_METAL_WATCHER_DEBUG_DELAY=<cycles>` plus
  `TT_METAL_{READ,WRITE,ATOMIC}_DEBUG_DELAY_CORES` and
  `TT_METAL_READ_DEBUG_DELAY_RISCVS`. Requires `TT_METAL_WATCHER` set and
  `TT_METAL_WATCHER_DISABLE_NOC_SANITIZE` **not** set — `rtoptions.cpp` asserts
  both. Note: ClaudeCurriculum's `docs/hangs.md` calls the first var
  `TT_METAL_WATCHER_DELAY`; `rtoptions.cpp` line 1833 reads
  `TT_METAL_WATCHER_DEBUG_DELAY`. Use the latter.
- **Watcher as a regression discriminator.** A test that passed without watcher
  but trips a CB-sanitizer overflow under `TT_METAL_WATCHER=10` is not a new
  regression — the overflow was always latent. A prior passing record without
  watcher is therefore not evidence the code was ever correct. Interval choice:
  **longer** for hangs that come and go (less disturbance to timing), **shorter** for
  deterministic ones. `watcher.md`'s table has this backwards by implication —
  it presents short intervals as the escalation for hard-to-reproduce bugs.
  Verify against `watcher.rst` before writing `tt-watcher`.
- **Binary-size shedding order** when watcher pushes fabric kernels over the
  limit: `_DISABLE_NOC_SANITIZE` → `_NOINLINE` → `_DISABLE_ASSERT`.

---

## Family 2 — Attach to a stuck run

These read the device directly — RISC callstacks out of mailboxes, NoC
registers, ARC, the code loaded on the cores. They are what you have left when
you turned nothing on beforehand, and the only tools that work on a process you
cannot restart.

| Tool | Verified source of truth | Activation | Existing coverage | Owner | Eval |
|---|---|---|---|---|---|
| **tt-exalens** | repo `tenstorrent/tt-exalens` — "a low level hardware debugger". CLI `tt-exalens.py`, lib `ttexalens/`, docs `ttexalens-app-docs.md`, `ttexalens-lib-docs.md`, `gdb.md`, JTAG tutorial (all **unread**) | `tt-exalens.py`; `--server` mode paired with `tt-triage.py --remote-exalens` | `incidental` — appears in `skills/debugger/triage.md` only as a version-pin trap. Everything triage does runs on top of it, yet no skill drives it directly | `tt-exalens` | — |
| **tt-triage** | `tools/tt-triage.py`, `tools/tt-run-triage.py`, `tools/triage/` (~20 scripts); `tools/triage/tt-triage.md` (**unread**); `docs/source/tt-metalium/tools/triage.rst` (**unread**) | `tt-triage.py --llm-output --triage-summary-path=…`; `--run=<script>`, `--dev=in_use\|all`, `-v`/`-vv`, `--all-cores`, `--initialize-with-noc1`, `--remote-exalens` | `covered` — `skills/debugger/{triage,scripts,interpretation}.md`. Deepest coverage in the repo; the reference model for the rest | `tt-triage` | — |
| **`watcher_dump` / GDB watcher dump** | `tt_metal/tools/watcher_dump/` (**unread**); `call tt::watcher::dump(stderr, true)` from a `tt::`-namespace frame | standalone binary, or GDB on a live/core-dumped process | `none` | `tt-watcher` | — |

Notes:

- **tt-exalens is the layer everything else stands on.** Triage is a collection
  of Python scripts over it; the version pin between them is enforced and triage
  refuses on mismatch. A `tt-exalens` skill is what makes the one-off question
  ("read this L1 address on that core") answerable without inventing a triage
  script. It also owns the JTAG and GDB paths, which nothing else covers.
- The `watcher_dump` row sits in family 2 on purpose: `tt::watcher::dump()` works
  **even when watcher was never enabled**. Debug-only state (waypoints) is
  absent, but mailboxes and HW registers still read. That makes watcher's data
  structures reachable post-mortem after a host-side assert or segfault — a
  genuinely different capability from watcher's live polling thread in family 1.
- **`scripts.md` is missing 6 live triage scripts:** `arc_heartbeat_sampling`,
  `check_l1_status`, `dump_configuration`, `dump_mesh_sockets`,
  `dump_watcher_ringbuffer`, `parse_inspector_logs`. (`check_cb_inactive` is in
  `interpretation.md` but absent from `scripts.md`'s funnel.) Verified against
  `tools/triage/` on `main`.
- **Three triage flags tt-buddy does not document:** `--dev=in_use` (the
  default) / `--dev=all`; `--initialize-with-noc1`, for when NOC0 is wedged;
  and `--remote-exalens`, which pairs with `tt-exalens --server` when UMD init
  fails because another process owns the device. The last is the only path to
  triage a device you cannot open.
- `dump_lightweight_asserts` and `dump_watcher_ringbuffer` are triage scripts
  that read what family-1 tools left behind. This is the chaining described in
  § How these are grouped: the skill that turns the tool on and the skill that
  reads the result are two different skills, and each has to point at the other
  or neither is any use.

---

## Family 3 — Read host runtime state

These read what the Metal runtime on the host knows — which program, which op,
which kernel binary, what the tensors are. This is the layer that turns a device
address into a name, which is why triage gets much less useful without it.

| Tool | Verified source of truth | Activation | Existing coverage | Owner | Eval |
|---|---|---|---|---|---|
| **Inspector** | `tt_metal/impl/debug/inspector/` (`data.cpp`, `logger.cpp`, `rpc.capnp`, `rpc_server_controller.cpp`); `docs/source/tt-metalium/tools/inspector.rst` (**unread**) | `TT_METAL_INSPECTOR` (on), `_RPC` (on, capnp at localhost:50051, rank-shifted under MPI), `_RPC_SERVER_ADDRESS`, `_CAPTURE_TENSOR_SPECS`, `_LOG_MESH_BUFFERS`, `_LOG_MESH_SOCKETS`, `_LOG_RUNTIME_ENTRIES`, `_INITIALIZATION_IS_IMPORTANT`, `_WARN_ON_WRITE_EXCEPTIONS` | `incidental` — 3 of 10 env vars listed as triage prerequisites. No direct RPC query, no log-dir reading | `tt-inspector` | — |
| **Inspector serialization** | same as Inspector (`data.cpp`) | `TT_METAL_INSPECTOR_SERIALIZE_ON_DISPATCH_TIMEOUT` (on) | `incidental` — one row in `skills/debugger/triage.md`'s env table | fold into `tt-inspector` | — |
| **Operation timeout + timeout hook** | `tt_metal/llrt/rtoptions.cpp` | `TT_METAL_OPERATION_TIMEOUT_SECONDS`, `TT_METAL_DISPATCH_TIMEOUT_COMMAND_TO_EXECUTE`, `TT_METAL_DISPATCH_PROGRESS_UPDATE_MS` | `covered` — tt-buddy `skills/run/execution.md` sets the timeout to 30s and wires the tt-triage callback | `tt-operation-timeout` | — |
| **dispatch_telemetry_dump** | `tt_metal/tools/dispatch_telemetry_dump/dispatch_telemetry_dump.cpp` (**unread**) | `TT_METAL_DISPATCH_TELEMETRY_DISABLE` (opt-out) | `none` | `tt-dispatch-telemetry` | — |

Notes:

- `TT_METAL_INSPECTOR=0` silently degrades triage: dispatcher-aware scripts
  skip, HW checks still run. Already flagged in tt-buddy's `triage.md`; `tt-inspector`
  must own the inverse — how to turn the knobs *up*.
- Inspector writes to `generated/inspector/`. Methods without arguments are
  auto-serialized at process exit; custom RPC methods extend via the
  `rpc.capnp` schema. `TT_METAL_INSPECTOR_INITIALIZATION_IS_IMPORTANT=1` makes
  init failure fatal — it fails open by default, which is how a run silently
  loses its triage path.

---

## Family 4 — Performance

| Tool | Verified source of truth | Activation | Existing coverage | Owner | Eval |
|---|---|---|---|---|---|
| **Tracy + tt-perf-report** | `tools/tracy/`; `tt_metal/tools/profiler/tt_metal_tracy.hpp`, `tracy_debug_zones.hpp`, `tracy_debug_categories.txt`; repo `tenstorrent/tt-perf-report`; `docs/source/tt-metalium/tools/tracy_profiler.rst` (**unread**) | `tracy.py -p -r -v` wrapper (sets `TT_METAL_DEVICE_PROFILER=1`) | `covered` — tt-buddy `skills/profiler/{SKILL,tracy,interpretation}.md` | `tt-profiler` | — |
| **Device Program Profiler** | `tt_metal/tools/profiler/kernel_profiler.hpp`, `noc_event_profiler.hpp`, `fabric_event_profiler.hpp`; `docs/source/tt-metalium/tools/device_program_profiler.rst` (**unread**) | `TT_METAL_DEVICE_PROFILER`, `_DISPATCH`, `_NOC_EVENTS`, `_NOC_EVENTS_RPT_PATH`; plus `TT_METAL_PROFILER_*` post-processing knobs | `partial` — reached through the tracy wrapper. Direct env-var use, NoC-event mode, and mid-run dump are undocumented | `tt-profiler` | — |
| **Hardware performance counters** | `tt_metal/tools/profiler/perf_counters.hpp` (**unread**); `tools/tracy/perf_counter_analysis.py`. **No docs page** — upstream doc gap | `python -m tracy --profiler-capture-perf-counters=all` (groups: `fpu pack unpack l1_0 l1_1 instrn all`; BH adds `l1_2..l1_4`), or `TT_METAL_PROFILE_PERF_COUNTERS` as an OR-bitfield (FPU=1, PACK=2, UNPACK=4, L1_0=8, L1_1=16, INSTRN=32; BH +64/128/256) | `none` | `tt-perf-counters` | — |

Notes:

- Device profiler, `TT_METAL_DPRINT_CORES`, and `TT_METAL_WATCHER` are mutually
  exclusive — they **share on-device SRAM**, and enabling more than one
  *silently corrupts* debug data. `skills/profiler/tracy.md` states the
  exclusion but not the mechanism or the corruption consequence. This is
  cross-family and must be stated identically in `tt-dprint` and `tt-watcher`
  here, and in tt-buddy's profiler skill, or the three will contradict each other.
- **Perf counters have a hardware mux constraint:** only ONE L1 bank per run.
  Setting >1 L1 bit in `TT_METAL_PROFILE_PERF_COUNTERS` **throws**. The
  `--profiler-capture-perf-counters` CLI transparently multiplexes (runs twice,
  merges); the env-var path does not. Recommended broad capture: `47`.
- Perf-counter interpretation is a real model, not a dump: fixed thread mapping
  (0 = unpack, 1 = math, 2 = pack), `utilization = req/ref`,
  `backpressure = (req−grant)/req`, and two one-number verdicts —
  `NOC vs Compute Balance` and `Compute-to-Unpack Ratio`. Arch-dependent
  (WH 135 counters / 4 packer engines; BH 154 / 1). L1 unpacker backpressure of
  75–100% is normal. This is enough substance to justify `tt-perf-counters` as
  its own skill rather than a profiler sub-file.

---

## Family 5 — System / fleet state

| Tool | Verified source of truth | Activation | Existing coverage | Owner | Eval |
|---|---|---|---|---|---|
| **tt-smi** | repo `tenstorrent/tt-smi` | `tt-smi -s` (status), `tt-smi -r` (reset), `tt-smi -glx_reset` (Galaxy 6U) | `partial` — tt-buddy `skills/run/recovery.md` + `knowledge/recipes/developer-setup.md`. Reset path covered; read-only health interpretation is not | `tt-smi` (drive and read; recovery procedures are out of scope) | — |
| **UMD topology** | `tt-umd/tools/topology.cpp`; `tt-umd/device/topology/topology_discovery*.cpp`; `tt-umd/tools/README.md` (read) | `./build/tools/umd/topology` — emits cluster descriptor (PCI/remote chips, eth connections, harvesting). Requires `-DTT_UMD_BUILD_TOOLS=ON` | `none` | `tt-umd-tools` | — |
| **UMD telemetry / tt-telemetry** | `tt-umd/tools/telemetry.cpp`; repo `tenstorrent/tt-telemetry` (device telemetry service) | `./build/tools/umd/telemetry` — polls ARC (AICLK, VCore, Power, Temp), configurable rate + output file | `none` | `tt-umd-tools` | — |
| **UMD system_health** | `tt-umd/tools/system_health.cpp` | `./build/tools/umd/system_health` — board types, chip IDs, unique IDs, eth link state | `none` | `tt-umd-tools` | — |
| **UMD harvesting** | `tt-umd/tools/harvesting.cpp` | `./build/tools/umd/harvesting` — Tensix/DRAM/ETH/PCIE harvesting masks + core coords across coordinate systems | `none` | `tt-umd-tools` | — |
| **tt-toplike** | repo `tenstorrent/tt-toplike` — Rust hardware visualizer | TUI | `none` | not built — see below | — |
| **tt-topology** | repo `tenstorrent/tt-topology` — **flashes** multiple NB cards to specific eth routing configs | CLI | `none` | **do not build** — see below | — |

Notes:

- **UMD tools need a build.** `cmake -B build -G Ninja -DTT_UMD_BUILD_TOOLS=ON`
  then `--build build --target umd_tools`. None of these binaries exist in a
  stock tt-metal workspace. Every UMD skill needs a "is it built" precondition
  and a recipe entry, or it will emit commands that do not run.
- **Triage already covers this ground, worse.** Triage's `device_telemetry`,
  `check_eth_status`, `check_arc`, and `check_noc_locations` overlap
  UMD telemetry / system_health / harvesting. The UMD tools are the
  higher-fidelity, standalone versions; triage's are the in-hang snapshot. Each
  skill must say which one to reach for and why, or the dispatch table becomes a
  coin flip.

---

## Family 6 — Tools found during the scan that are not on the list

Surfaced from `ClaudeCurriculum/docs/{hangs,pcc,profiling}.md` and tt-metal's
own docs. All verified to exist. Now decided: graph capture and
comparison mode share `tt-ttnn-debug-modes`; `tt-npe` and `tt-ttsim` get their
own; `profile_this.py` and the mid-run dump fold into `tt-profiler`; the in-kernel
helpers fold into `tt-checkpoint`; the visualizer's report generation folds into
`tt-npe`; `tt-flash` gets a recipe line. NaN/Inf detection is **MISSING** — a
technique with no verified tool surface.

| Tool | Verified source of truth | Activation | Why it matters |
|---|---|---|---|
| **TTNN graph capture (hanging-op localization)** | `ttnn.graph.begin_graph_capture()` / `end_graph_capture()` | Requires `enable_fast_runtime_mode=false` via `TTNN_CONFIG_OVERRIDES` — **silently inert** in default fast mode. Pair with `TT_METAL_OPERATION_TIMEOUT_SECONDS` | Names the hanging op from the host side: orphan ops (`function_start` with no `function_end`) surface as `incomplete_operation`. The only tool here that localizes a hang without touching the device |
| **TTNN comparison mode + operation tracing** | `TTNN_CONFIG_OVERRIDES` | fast-runtime-mode debug modes | Per-op golden comparison. The correctness counterpart to graph capture; catches the op that diverges, not the op that hangs |
| **`print_cb_details` / DST dumps** | tt-metal kernel debug helpers | in-kernel | CB pointer + DST-register inspection at a stall. Sits between DPRINT and watcher |
| **NaN/Inf special-value detection** | `ClaudeCurriculum/docs/pcc.md` § NaN/Inf | (**unread**) | Cheap first pass on garbage output |
| **tt-npe** | repo `tenstorrent/tt-npe` — NoC performance estimator | `tools/tracy/profile_this.py --collect-noc-traces` → `npe_viz/` | The NoC-congestion tool. Nothing in tt-buddy covers NoC-bound analysis |
| **TT-NN Visualizer** | repo `tenstorrent/ttnn-visualizer`; `pip install ttnn_visualizer`, serves `0.0.0.0:8000` | `TTNN_CONFIG_PATH` JSON (`enable_logging`, `enable_detailed_buffer_report`) | Web UI. Agent-drivable only as a report *generator*; the UI itself is not readable by an agent — same class as tt-toplike |
| **`profile_this.py`** | `tools/tracy/profile_this.py` | `-c <cmd> -n <name> -o <out> --collect-noc-traces` | The **recommended** TTNN profiling entry point. `skills/profiler/tracy.md` drives `python -m tracy` instead — check which one the recipe should use |
| **Real-time / streaming profiler** | `TT_METAL_PROFILER_MID_RUN_DUMP` | env | Mid-run dumps for long serving jobs |
| **Wall-clock timers in kernels** | `ClaudeCurriculum/docs/profiling.md` § Wall Clock Timers (**unread**) | in-kernel | Sub-op timing without the device profiler's SRAM cost |
| **ttsim (simulator)** | `ClaudeCurriculum/docs/simulator.md`; `ttsim-private/src/tensix.cpp` as executable ISA spec | — | Deterministic. A logic-level deadlock reproduces on the first try; an intermittent silicon hang either reproduces instantly or is proven timing-dependent. **No hang detection under sim** (kHz speeds), so the watcher/triage flow does not apply. Also the only way to test an arch you cannot reserve |
| **tt-flash** | repo `tenstorrent/tt-flash` — firmware update utility | CLI, often not on `PATH`, needs sudo | Not a debug tool, but it is the *only* fix for two failure modes that look like hangs: harvesting-mismatch `TT_FATAL` and firmware-bundle version mismatch. Both survive `tt-smi -r` |

Two of these are misdiagnosis traps and belong in `tt-smi`'s Traps section,
because both look like hangs and survive a reset:

- **Harvesting mismatch** (`Number of harvested Tensix mismatch across devices`,
  `validate_harvesting_masks`) is an eFuse/ARC value. It survives reset, is
  identical across UMD versions, and `FAULTS=0x0` healthy telemetry misses it.
  Bad card / bad reservation — needs a power-cycle or `tt-flash`, not a retry.
- **Firmware-bundle mismatch** at `topology_discovery.cpp` (`create_ethernet_map`)
  means no device enumerates. `git submodule update` and `tt-smi -r` do not fix
  it; a reset re-inits but does not reflash. Distinguish from a busy device by
  the *absence* of orphaned tracy/pytest processes.

---

## Fixtures — the upstream test suite

tt-metal already ships the fixtures. `tests/tt_metal/tt_metal/debug_tools/` holds
roughly 145 gtest cases whose purpose is to force the device into a failing state
and assert on the exact string the tool produces. Consume these rather than
authoring repros.

| Binary | Built to | Covers |
|---|---|---|
| `unit_tests_debug_tools` | `build/test/tt_metal/` | `watcher/` (~40 cases) and `device_print/` (~55 cases) |
| `unit_tests_inspector` | `build/test/tt_metal/` | 4 RPC startup cases |
| `unit_tests_noc_debugging` | `build/test/tt_metal/` | named by `noc_debug_dump.rst` with its exact filter and full expected output |

How they assert, which is what makes them reusable as eval ground truth:

- `watcher/test_sanitize.cpp` builds the expected string with `fmt::format` and
  does `EXPECT_EQ` against the host exception. It covers 17 fault modes — bad
  coordinate, misalignment on read and on write, mailbox overwrite, L1 overflow
  and straddle, invalid transaction ID, multicast out of range, stateful-write
  bad coord, and more.
- The other watcher cases use `FileContainsAllStrings` / `FileContainsAllStringsInOrder`
  against `watcher.log`. The fixture polls at 250 ms and writes
  `generated/watcher/watcher.log`.
- `device_print/` covers every scalar type, format specifiers, tile printing,
  config registers, concurrent RISCs, in-kernel callstacks, and a
  `DevicePrintFailuresFixture` for compile-time format errors.
- `DevicePrintCheckpointTest` has 6 cases: `BasicCheckpoint`, `DumpCB`,
  `DumpCBTyped`, `DumpL1`, `GlobalCheckpoint`, `CheckpointLoopAndDumpDest`.

Per-tool activation commands and the output each one produces are in the
published reference page; the plan's per-skill `manifest.yaml` records the exact
case names each skill claims.

No fixture exists for: LLK sanitizer, tt-exalens, `dispatch_telemetry_dump`,
TTNN graph capture, comparison mode, tt-npe, ttsim, hardware perf counters.

## Tools that do not become skills

Four, and the reason is the same in each case: there is nothing an agent can
usefully drive, or driving it is not debugging.

1. **`tt-topology` flashes firmware routing config across a multi-card host.**
   Destructive, system-wide, hard to reverse, and not a diagnostic. A recipe line
   that hands the command to a human.
2. **`tt-toplike` is an interactive TUI.** An agent cannot read a live Rust TUI
   through a pipe.
3. **`tt-flash` needs sudo and the wrong bundle makes things worse.** Recipe
   line. Its two failure signatures — harvesting mismatch and firmware-bundle
   mismatch — belong in `tt-smi` because both look like hangs and survive a
   reset.
4. **TT-NN Visualizer is a web UI.** Only its report generation is drivable, so
   that folds into `tt-npe`.

Two foldings rather than exclusions: Inspector serialization is a flag on
Inspector, so it lives in `tt-inspector`; `print_cb_details`, DST dumps and
wall-clock timers are in-kernel helpers that `checkpoint.rst` already documents
alongside `debug_dump_cb`, so they live in `tt-checkpoint`.

Everything else is covered — including Tracy, the Device Program Profiler and
the operation-timeout hook, which earlier revisions skipped on the grounds that
tt-buddy skills cover them. `tt-debug-tools` is standalone and covers its own
tools.

That lands at **17 skills**. The merge rule, the shared body shape, the
per-skill budget, and the landing order are in `debug-skills-plan.md`.

## Prior art reviewed

| Source | Shape | What to take |
|---|---|---|
| `tt_ops_code_gen/agents/ttnn-expert-debugger.md` | Fresh-context subagent, 3-hypothesis cap, git-commit-per-step audit trail | The DEVICE_PRINT reference is the best that exists: TSLICE on TRISC vs the full `TileSlice` ctor required on BRISC/NCRISC, the 32-value truncation, row-by-row loop, non-destructive peek, CB push-counter pattern, deterministic input table. Lift into `tt-dprint`. Fix the `TT_METAL_DEVICE_PRINT=1` error. |
| `tt_ops_code_gen/skills/debug-ttnn-op/SKILL.md` | Methodology reference keyed off exit code (1 = fail, 2 = hang) | Triage grep-target table; the "cryptic TRISC error ⇒ init/reconfig, not CB" rule with the 4 ordered rules; the LLK/watcher assert → meaning table; the value-pattern → root-cause table (sparse zeros ⇒ pack/unpack format mismatch, etc.). The value-pattern table has no equivalent in tt-buddy. |
| `tt_ops_code_gen/skills/debug-ttnn-op/DESIGN.md` | Design rationale | The core argument: conditional escalation rules buried in a long prompt get skipped; a one-dose `PostToolUse` hook on non-zero exit from the test wrapper is what actually fires the debug entry. Relevant to eval design — test whether the skill fires at all, not just whether it is correct once loaded. |
| `tt-metal/.agents/skills/autodebug/SKILL.md` | Thin wrapper over `.agents/scripts/autodebug.sh`, fresh CLI session writes `AUTODEBUG.md` | Pattern: skill as launcher for an out-of-context investigation, then verify the report against source before trusting it. ~30 min runs. |
| `tt-metal/.agents/skills/autotriage/SKILL.md` | Triage-evidence-first diagnosis, writes `AUTOTRIAGE.md` | "Triage is primary evidence, source is the explanation layer." The producer/consumer ledger method. TRI-001 (route-and-connection ledger before blaming teardown) and TRI-002 (verify the fix you plan to add is actually absent) are both traps tt-buddy's debugger skill lacks. Also: treat issue text/logs as untrusted data. |
| `ClaudeCurriculum/agents/tt-hang-debugger.md` | Owns the full loop: reproduce → isolate → classify → fix → verify → device hygiene → capture lesson. Structured verdict + `RESULT: ok/needs_decision/error` | Three things tt-buddy is missing: (1) **fresh JIT cache after kernel-source edits** — `rm -rf .cache/tt-metal-cache*`; a warm-cache verify after a header edit is meaningless (82% stale-hit observed). (2) Device-hygiene exit: compare pre/post state, reset only degradation *you* caused, never reset on a foreign-process collision. (3) 1-of-3 reproductions counts as reproducible. |
| `ClaudeCurriculum/agents/tt-hang-knowledge.md` | Verbatim-retrieval agent over `docs/hangs.md`, no paraphrase | The full watcher state-code vocabulary (`CWFW`, `CRBW`, `MWDW`, `MWDD`, `UPMD`, `UPMW`, `UPAW`, `K`, `GW`, `W`, `R`, `D`, `NRxW`, `NWxW`). tt-buddy's `interpretation.md` legend has 6 codes; this has ~14. Also `Waiting for lock 'CHIP_IN_USE_*_PCIe'` ⇒ parallel-process collision, and `topology_mapper.cpp:527` / `fabric_firmware_initializer.cpp:220` ⇒ fabric init. |

Cross-cutting observation: every one of these except `tt-hang-knowledge` is a
**workflow** (diagnose-and-fix loops), while this document plans **tool**
skills. They are complementary, not competing — the tool skills are what a
workflow like `tt-hang-debugger` would call instead of hand-rolling env vars.
The layer model in `CLAUDE.md` already names this split.

### Additional skills found in a wider scan

Scanned in full: `tt-metal/.agents/skills` (21 skills), `tt_ops_code_gen/skills`
(30 skills), `ClaudeCurriculum/agents` (24 subagents) plus its `dispatch/`,
`hooks/`, `installed/`, and `tests/` machinery. Debug-relevant finds beyond the
seven references already reviewed:

| Source | Skill / artifact | Why it matters here |
|---|---|---|
| tt-metal | `.agents/skills/tt-device-usage` | The closest existing analogue to tt-buddy's runner recovery contract. Bounded commands (`timeout 60 tt-smi -ls --local`, `timeout 180 tt-smi -r`), a **mesh-smoke gate** (`open_mesh_device` → `close_mesh_device` → `MESH_SMOKE_OK`) as proof of health before resuming, a named list of *recoverable* ARC/ERISC/remote-Ethernet signatures, and "a stage may not declare itself blocked until recovery fails". tt-buddy's `skills/run/recovery.md` should adopt the mesh smoke and the two-resets-before-escalating rule — out of scope for this plugin. |
| tt-metal | `.agents/skills/autofix` | The loop that consumes `AUTOTRIAGE.md` / `AUTODEBUG.md`: test each proposed bug **in isolation**, keep proven fixes, refute wrong hypotheses, re-run diagnosis with new evidence. The missing third piece of the autodebug/autotriage pair. |
| tt-metal | `.agents/skills/optimize`, `graph-fusing`, `multichip`, `tt-enable-tracing` | Overlap `tt:optimizer`. `tt-enable-tracing` covers a failure class tt-buddy has nothing on: trace capture/replay faults (unsupported writes, stale inputs, event sync, bad replay correctness). |
| tt-metal | `.agents/skills/stage-review` | Independent fresh-`xhigh` reviewer that gates stage closure against the goal contract and evidence, explicitly hunting "weak dismissals". A sharper framing than `tt:code-review`'s five reviewers. |
| tt_ops_code_gen | `skills/eval-dev`, `eval-launch`, `list-runs`, `analyze-runs`, `manual-ingest`, `nuke-op` | **A complete agentic eval system.** See § Eval plan — this is the most directly relevant find in the whole scan. |
| tt_ops_code_gen | `skills/perf-measure`, `perf-ceiling-dm`, `perf-lab` | `perf-ceiling-dm` computes a data-movement *ceiling* from a proposed NoC-transfer algorithm and chases it — `tt:optimizer` has a loop but no target model. `perf-lab` authors isolated on-device A/B micro-benchmarks. |
| tt_ops_code_gen | `skills/numeric-formats-metal`, `memory-budget-metal`, `padding-in-ttnn`, `memory-layouts` | `debug-ttnn-op` hands precision bugs off to `numeric-formats-metal`; `memory-budget-metal` owns the L1-OOM signature. Both are the "fix" half of diagnoses our debugger will produce. |
| ClaudeCurriculum | `agents/tt-pcc-debugger` + `docs/pcc.md` | The wrong-output counterpart to `tt-hang-debugger`. `docs/pcc.md` is where DPRINT/DEVICE_PRINT value inspection actually lives, plus the fresh-JIT-cache rule and arch-delta-vs-regression discrimination. **Read this before writing `tt-dprint`.** |
| ClaudeCurriculum | `agents/tt-cross-arch-verifier` | Runs a target test under ttsim on an arch the host lacks, and classifies the result — including a distinct `sim_clean` outcome that must not be conflated with `ok`. |
| ClaudeCurriculum | `agents/tt-baseline-verifier` | Normalizes branch / submodule / build / device state *before* any task. `check_stale_binary.sh` is its build-freshness primitive. tt-buddy has no equivalent, and a stale `_ttnn.so` silently reproduces already-fixed bugs. |
| ClaudeCurriculum | `agents/tt-l1-budget-analyzer`, `tt-perf-validator`, `tt-rebase-helper` | Symptom-dispatched on `l1_overflow`, `tt_fatal_oom`, `expected_perf_out_of_band`. |
| ClaudeCurriculum | `dispatch/symptoms.yaml` (52 entries) | Symptom **regex** → subagent, with `hard`/`soft` enforcement. The four `hard` hang entries: `safe_pytest_hang`, `cb_wait_deadlock`, `chip_in_use_lock`, `fabric_topology_failure`. This is tt-buddy's dispatch table made mechanical. |
| ClaudeCurriculum | `dispatch/forcing-rules.yaml` (25 rules) | Discipline **enforced at the tool call**, not requested in prose. Directly relevant: `watcher_on_pytest` (injects `TT_METAL_WATCHER=10` into any device pytest), `tracy_no_dprint` / `device_profiler_with_dprint` (inject an unset prefix — the SRAM mutex, enforced), `llk_asserts_without_reporter`, `pytest_hang_guardrail` (wraps the command), `manual_watcher_grep` / `manual_perf_csv_awk` / `manual_pcc_grep` (`recommend_subagent`). |
| ClaudeCurriculum | `installed/tools/*.sh` | Deterministic primitives with exit-code contracts instead of prose: `recover_user_hang.sh` (kills only your own orphans, **refuses** if a matching proc has a live parent — exit 3 `live_parent`), `check_stale_binary.sh` (`fresh`/`stale`/`not_built`), `tracy_op_durations.sh`. |

The last three rows are the important ones. tt-buddy's Red Flags table asks the
model not to hand-grep `watcher.log`; `forcing-rules.yaml` makes hand-grepping
*return a recommendation instead of output*. Same intent, different enforcement
strength — and tt-buddy already ships a `hooks/` directory, so the mechanism is
available.

---

## Eval plan

Owned by `debug-skills-plan.md`. Three tiers: a static declaration join per
PR, a dispatch-and-command-shape eval with a model but no device, and an
interpretation eval against real artifacts on hardware. The fixtures for the
third tier mostly exist already — see § Fixtures.

## Open questions on the tool facts

Build-state questions live in the plan. These are about the tools themselves.

- Fabric telemetry (`TT_METAL_FABRIC_TELEMETRY`, `TT_METAL_FABRIC_BW_TELEMETRY`)
  exists in `rtoptions.cpp` and is on no tool list. Own it under
  `tt-dispatch-telemetry`, give it a fabric skill, or drop it.
- `skills/profiler/tracy.md` in tt-buddy drives `python -m tracy`, while
  tt-metal's own docs call `tools/tracy/profile_this.py` the recommended TTNN
  entry point. One of the two is wrong for TTNN work.
- Which UMD tools need a build recipe entry versus living entirely inside the
  skill. All four need the `-DTT_UMD_BUILD_TOOLS=ON` gate stated somewhere.
- Whether the `watcher.rst` interval guidance contradicts tt-buddy's table, which
  presents short intervals as the escalation for hard-to-reproduce bugs.
