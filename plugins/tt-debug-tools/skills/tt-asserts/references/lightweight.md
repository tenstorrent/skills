# Lightweight kernel asserts

Your own assertions in kernel code. Designed to cost almost nothing when they
pass, so they are the family to reach for on a hot path.

```bash
export TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS=1   # default 0
```

Default off. With it off the macro compiles out, so an `ASSERT` left in a kernel
costs nothing in a normal build — and equally, a build without this variable will
sail past a condition you thought was guarded.

## What firing does

`ASSERT` expands to an `if` plus a RISC-V `ebreak`. On failure the core halts and
enters debug mode. Nothing is printed. The host keeps waiting on a command-queue
completion that will never arrive, so from outside the run simply stops making
progress.

That is the whole reason this family and `tt-triage` are two skills that have to
point at each other: this one puts the device in the state, that one reads it.

## Reading it back

```bash
tools/tt-triage.py --run=dump_lightweight_asserts
```

The script prints the call stack for each failed assertion. Upstream's own note
is that this family gives *more* detail than LLK asserts at present, and the
dump covers callstacks, template parameters, runtime arguments and locals.

Two ways to run it:

- **After the fact**, against the still-hung process. Keep the process alive;
  triage against an exited one degrades badly.
- **Automatically**, by wiring the dispatch timeout to run it — which is what
  `tools/setup_llk_assert_env.sh` does. See `references/llk-asserts.md`.

## As a reporter for other families

This family doubles as the reporting path for LLK asserts. With
`TT_METAL_LLK_ASSERTS=1` and this variable both set, an LLK assertion failure
becomes readable through `dump_lightweight_asserts` rather than being a bare
`ebreak`. The alternative reporter is watcher, and the two are not
interchangeable — watcher prints a message immediately, this one requires a
triage pass.

## Traps

**A debugger can attach instead.** The core is in debug mode, not dead. Where a
callstack is not enough, `tt-exalens` owns the GDB path.

**It does not replace watcher's asserts.** Upstream positions this as a
low-overhead complement, not a substitute: watcher's assert reporting carries
more context and prints without a triage step.

**Default off is the common failure.** An `ASSERT` that never fires in a run
where this variable was unset proves nothing about the condition. Check the
environment before concluding the assertion held.
