# LLK asserts

Runtime checks inside tt-llk, the library that implements the compute stack.
They validate assumptions the library makes about tensor dimensions, data
formats and hardware configuration, during:

- unpacking — L1 into source registers
- math — matmul, element-wise
- packing — destination register back to L1
- tilization and untilization

They run on the accelerator, and they are controlled independently of lightweight
asserts and of watcher.

```bash
export TT_METAL_LLK_ASSERTS=1   # default 0
```

## Pick a reporter

Legal on its own, but on failure you get an `ebreak` and a hang with no message.
Three ways to get a report:

| Combination | What you get |
|---|---|
| `TT_METAL_LLK_ASSERTS=1` alone | The failing condition, callstack, template parameters and locals through triage. Default. |
| `TT_METAL_LLK_ASSERTS=1` + `TT_METAL_WATCHER=1` | The assertion message on stderr and in `watcher.log`. |
| `TT_METAL_LLK_ASSERTS=1` + `TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS=1` | Upstream's recommended pair. **Fails to build under ttnn fabric dispatch on Wormhole n300; measured no richer than LLK-only.** |

Upstream recommends the pair because lightweight asserts are supposed to give
more detail. Measured on Wormhole n300 at `058b93d450f`, the pair overflows
`idle_erisc`'s code region once ttnn brings up fabric dispatch:

```
kernels/cq_prefetch/.../idle_erisc.elf: segment[0] overflows region:0
```

A raw `metal_example_loopback` under both flags is fine, so the recommendation
looks correct until a real workload uses it. And it bought nothing: on the LLK
flag alone, `dump_lightweight_asserts` gave the failing condition, callstack,
template parameters and locals — triage recovers those from the ELF, not from
anything runtime records.

Add the lightweight flag only if an assert reports less without it.

## The instrumented one-liner

`tools/setup_llk_assert_env.sh` turns the hang into a self-triaged failure.
**Source it** — it works by exporting into your shell — and give it two required
paths. It needs `TT_METAL_HOME`, which activating the Python environment
provides.

```bash
cd $TT_METAL_HOME && source python_env/bin/activate
source tools/setup_llk_assert_env.sh assert.txt /tmp/tt_dprint.log
TT_METAL_LLK_ASSERTS=1 pytest <test> > test_output.txt
```

What it exports:

| Variable | Purpose |
|---|---|
| `TT_METAL_DISPATCH_TIMEOUT_COMMAND_TO_EXECUTE` | On timeout: `tt-triage.py --run=dump_lightweight_asserts > <assert_out> && tt-smi -r` |
| `TT_METAL_OPERATION_TIMEOUT_SECONDS=5.0` | Short completion timeout, so the hang is declared in seconds rather than minutes |
| `TT_RUN_DISABLED_TRIAGE_SCRIPTS_IN_CI=1` | Lets the triage scripts run where they are otherwise gated off |
| `TT_METAL_DPRINT_CORES=all` | Subscribes the host print server to every core, so TRISC prints are captured |
| `TT_METAL_DPRINT_FILE=<dprint_out>` | Sends that output to a file |
| `TT_METAL_DEVICE_PRINT=1` | **Nothing. This variable does not exist** — see Traps |

## The flow

1. An `LLK_ASSERT` condition fails on a TRISC, `ebreak`, that core hangs.
2. Five seconds later the host declares a dispatch timeout.
3. The host runs the timeout command: triage walks the device, finds the
   asserting TRISC, recovers callstack, template params, runtime args and
   locals, writes them to your assert file.
4. `tt-smi -r` resets the device.
5. Pytest reports a `RuntimeError`.

Three artifacts, and between them the failing kernel, line and mismatched
values, in one run and with no rebuild: `test_output.txt`, the assert dump, and
the DPRINT log — where `expected: …, actual: …` lines emitted near the failing
assert end up.

## Traps

**`TT_METAL_DEVICE_PRINT=1` in the script does nothing.** It is not an
`EnvVarID`; the variable that enables printing is `TT_METAL_DPRINT_CORES`, which
the script also sets. The line is inert, and copying it out as "the print switch"
is a mistake upstream's own script invites.

**The script's `TT_METAL_DPRINT_CORES=all` collides with watcher.** DPRINT and
watcher share on-chip SRAM. Choosing watcher as the reporter *and* sourcing this
script silently corrupts both.

**The timeout command resets the device.** `tt-smi -r` is board-level and hits
every tenant on the host. On a shared machine, know that before sourcing.

**A 5-second operation timeout will fire on slow legitimate work too.** It is
tuned for provoking the assert path, not for a normal run — do not leave it
exported into an unrelated session.
