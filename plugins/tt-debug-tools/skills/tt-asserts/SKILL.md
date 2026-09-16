---
name: tt-asserts
description: Turn on assertion checking inside Tenstorrent kernels — lightweight kernel asserts, LLK asserts in the compute library, and the LLK sanitizer — and read back what fired. Use when a kernel produces impossible results, when you suspect a broken assumption about tile dimensions or data formats, or when a run hangs and you want to know whether an assert caused it. A fired assert halts the core and looks exactly like a hang from outside.
metadata:
  tier: kernel
  upstream:
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: docs/source/tt-metalium/tools/lightweight_kernel_asserts.rst
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: docs/source/tt-metalium/tools/llk_asserts.rst
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tools/setup_llk_assert_env.sh
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/tt-llk/common/sanitizer/output.h
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/llrt/rtoptions.cpp
---

# tt-asserts

Three independent assertion mechanisms, one decision. They are folded together
because turning one on without knowing how it reports wastes the run — and
because the reporting paths interact.

| Mechanism | Checks | Enable |
|---|---|---|
| Lightweight kernel asserts | Your own `ASSERT(...)` in kernel code | `TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS=1` |
| LLK asserts | tt-llk's own assumptions — tile dims, data formats, hardware config, during unpack/math/pack/tilize | `TT_METAL_LLK_ASSERTS=1` |
| LLK sanitizer | Deeper tt-llk invariants, by severity | `TT_METAL_LLK_SANITIZER=1` |

## When to invoke

- A kernel returns impossible values and you want the library's own assumptions
  checked rather than guessing which one broke.
- You want an `ASSERT` in your kernel to actually stop the run.
- A run hangs and you need to know whether an assert caused it.

Not this skill: reading the halted state itself — `tt-triage`. Printing values
rather than asserting on them — `tt-dprint`.

## Surface

```bash
TT_METAL_LLK_ASSERTS=1 pytest <test>
```

Upstream pairs that with `TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS=1`. Measured on
Wormhole, the pair overflows the dispatch build under ttnn and adds no detail —
`references/llk-asserts.md`.

The sanitizer adds six independent severity toggles —
`TT_METAL_LLK_SANITIZER_{PEDANTIC,WARN,ERROR,FAULT,INFO,INTERNAL}=1`. Each is a
switch, not a threshold, so setting `ERROR` does not imply `FAULT`.

Per-family detail, and what each reports where:
`references/lightweight.md`, `references/llk-asserts.md`,
`references/sanitizer.md`.

## Force the state

The instrumented path is a one-liner upstream ships. **Source it, do not execute
it** — it works by exporting into your shell, and it takes two required paths:

```bash
cd $TT_METAL_HOME && source python_env/bin/activate
source tools/setup_llk_assert_env.sh assert.txt /tmp/tt_dprint.log
TT_METAL_LLK_ASSERTS=1 pytest <your test> > test_output.txt
```

That wires a 5-second dispatch timeout to a triage command, so a silent `ebreak`
becomes three files: the host `RuntimeError`, the device assert dump, and the
DPRINT log. Mechanism in `references/llk-asserts.md`.

## Output

**Nothing is printed at the moment an assert fires.** The macro expands to an
`if` plus a RISC-V `ebreak`: the core halts and enters debug mode, the host waits
on a command-queue completion that never arrives, and what you see is a hang.

Where the diagnosis comes from depends on which reporter is enabled:

| Enabled alongside | What reports the failure |
|---|---|
| Lightweight asserts | `tt-triage.py --run=dump_lightweight_asserts` — callstacks, template params, runtime args, locals |
| Watcher | The assertion message on stderr and in `watcher.log` |
| Neither | Still `tt-triage`, but with less detail |

The sanitizer is the exception: it prints, prefixed `llk::san | <severity> |`.

## Traps

**A fired assert is indistinguishable from a hang.** Dispatch times out, triage
runs, and nothing says "assert" until you look. Any investigation that reaches
for a hang workflow should check the asserts it had enabled before blaming the
NoC.

**Enabling an assert family is not enabling a report.** `TT_METAL_LLK_ASSERTS=1`
on its own gives an `ebreak` with no runtime message, but triage recovers the
assert expression, callstack, template parameters and locals from the ELF, so
the lightweight-asserts pairing that upstream documents is not needed for
detail. Measured on Wormhole n300 the pairing also overflows the dispatch
build under ttnn. Prefer LLK asserts alone; use watcher only if you want the
message on stderr and in `watcher.log`. See `references/llk-asserts.md`.

**The sanitizer will not compile on its own.** With `LLK_SAN_ENABLE` set but
neither LLK asserts nor device print enabled, the build stops with a `#error`.
It is a compile-time dependency, not a silent degradation.

**`setup_llk_assert_env.sh` exports `TT_METAL_DEVICE_PRINT=1`, which does not
exist.** It is not in the `EnvVarID` list and is ignored; printing is enabled by
the `TT_METAL_DPRINT_CORES=all` the script also sets. Do not copy that line out
as if it were the switch — see `tt-dprint`.

**The setup script turns DPRINT on for every core, which collides with watcher.**
DPRINT and watcher share on-chip SRAM. Using watcher as the reporter *and* the
script together silently corrupts both, so choose one reporter.

**`tt-smi -r` is part of the script's timeout command.** It resets the device
after dumping. On a shared host that hits every tenant — know it is there before
running it.
