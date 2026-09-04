---
name: tt-triage
description: Read the state of a hung or stuck Tenstorrent device with tt-triage — which op was running, where each RISC is parked, whether the on-device code and mailboxes are intact, whether NoC, Ethernet and ARC are healthy. Use when a job hangs, a test stops making progress, an assert has fired, or a triage report needs interpreting. Attach to the live process; do not kill it first.
metadata:
  tier: process
  upstream:
    - repo: tenstorrent/tt-metal
      ref: f9f5f3e080c4ed3d09f7fa70da81316724fa298a
      path: docs/source/tt-metalium/tools/triage.rst
    - repo: tenstorrent/tt-metal
      ref: f9f5f3e080c4ed3d09f7fa70da81316724fa298a
      path: tools/tt-triage.py
    - repo: tenstorrent/tt-metal
      ref: f9f5f3e080c4ed3d09f7fa70da81316724fa298a
      path: tools/triage
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: skills/debugger/triage.md
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: skills/debugger/scripts.md
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: skills/debugger/interpretation.md
---

# tt-triage

About twenty Python scripts that read device state in one pass and print one
section per script. Runs on top of tt-exalens and needs Inspector to have been
running in the workload. This is the first thing to reach for on a hang.

Needs Python 3.10 or newer and
`python -m pip install -r tools/triage/requirements.txt`.

## When to invoke

- A job or test hangs, stops making progress, or times out.
- An assert fired — a failed assert halts the core with `ebreak` and looks
  exactly like a hang from outside, so the assert is read through triage.
- You have a triage report and need it interpreted.

Adjacent skills: `tt-exalens` for reading one address or register directly, and
for JTAG or GDB. `tt-watcher` when the workload was launched with watcher on and
`watcher.log` already holds the answer.

## Run it

**Keep the hung process alive.** Triage reads it over a live Inspector RPC.
Against a dead process the output degrades to almost nothing.

```bash
tools/tt-triage.py --llm-output \
  --llm-output-path=out.txt \
  --triage-summary-path=summary.txt
```

Re-check one thing after a full pass:

```bash
tools/tt-triage.py --llm-output --run=dump_callstacks
```

Multi-process or multi-host, one pass per rank merged into one stream:

```bash
tools/tt-run-triage.py --rank-binding=<bindings.yaml> -- --llm-output
```

`--llm-output` gives CSV instead of Rich tables — cheaper to read and greppable,
so use it always. Two flags matter when the normal path fails.
`--initialize-with-noc1` when NOC0 is wedged. `--remote-exalens` when UMD cannot
initialise because another process owns the device — this one needs
`tt-exalens --server` already running in another shell, started before the
workload. Full flag set: `references/flags.md`.

## Force the state

Any live run is inspectable, but a halted core is what makes the callstacks
interesting. The watcher suite provides one on purpose:

```bash
build/test/tt_metal/unit_tests_debug_tools \
  --gtest_filter=MeshWatcherFixture.TensixTestWatcherSanitize
```

## Output

`summary.txt` is one line per script, `name: pass | FAIL — message`. `out.txt`
holds one CSV section per script, each headed by the producing script name with a
column header row and one row per core or RISC.

```
dump_callstacks.py:
RISC-V,Loc,Kernel Name,...,Kernel Callstack
NCRISC,"(1,1)",reader_kernel,...,"#0 ... cb_wait_front ..."
```

Read it top-down: `references/read-order.md`. What each script tells you:
`references/scripts.md`. Stuck frame or failed check to a likely cause:
`references/signals.md`.

## Traps

**Op-level scripts report the dispatcher, not the cores.** An op reads idle when
its GO was already sent and the dispatcher moved on, even while a kernel is still
physically stuck. An idle op level does not mean an idle device. Callstacks are
ground truth.

**Check integrity early.** `check_core_magic` and `check_binary_integrity` decide
how much of the rest you can believe: a corrupted mailbox invalidates every
script that reads mailboxes, callstacks included.

**Triage halts cores to read them** and may not resume them all, so a handful of
"broken" cores from `check_broken_components` is its own artifact. It matters
only when nearly every core is reported.

**Empty or skipped sections usually mean one provider failed.** Commonly
Inspector. One upstream failure blanks its dependents; find the first
non-skipped provider error, fix that, and re-read.

**It refuses on a tt-exalens version mismatch.** Surface the error rather than
working around it; `--skip-version-check` exists but you own what follows.

**`TT_METAL_INSPECTOR=0` in the workload degrades this silently** — the
dispatcher-aware scripts skip and only hardware checks run. Report the
degradation instead of reading the thin report as a clean bill of health.
