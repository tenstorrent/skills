---
name: autofix
description: Tenaciously repair difficult tt-metal and TTNN bugs after AutoDebug or AutoTriage has produced a diagnosis. Use automatically when the user asked for a fix and the failure needs experiments that prove or disprove competing hypotheses until the root cause is found and addressed. Keep fixes minimal and report unverified hardware claims explicitly.
---

# AutoFix

Turn a supported diagnosis into the smallest verified repair. Start from `AUTODEBUG.md` or
`AUTOTRIAGE.md`; if neither exists or the causal chain is weak, run `$autodebug` first.

## Repair loop

1. Restate the failing contract and the observations the proposed cause must explain.
2. List plausible fixes in priority order. Prefer the earliest inconsistent calculation or state
   transition over suppressing the downstream assertion, allocation, waiter, or teardown symptom.
3. For each hypothesis, identify one discriminating check. When the host supports isolated agents,
   give each agent one hypothesis to prove or refute; do not let parallel agents edit the same file.
4. Make the minimum source change that restores the contract. Do not mix cleanup or unrelated
   improvements into the fix.
5. Match validation to the change: run focused host tests or builds first, then the smallest
   relevant device test when hardware and authorization are available.
6. If a runtime observation is required but hardware is unavailable, state the exact remaining
   uncertainty. Never claim a device result or performance improvement without measurement.
7. Write `AUTOFIX.md` with the diagnosis, changed files, validation commands and results, and any
   unresolved risk.

## Useful discriminating experiments

- Accuracy: compare intermediate tensors at the first divergence; sweep dtype, layout, shape,
  padding, and program-cache reuse independently.
- Hangs: preserve the live state, compare producer/consumer counts, and verify completion and
  synchronization boundaries before changing teardown behavior.
- Caches and traces: compare cold, warm, and replayed program identities; check that runtime-mutated
  values are not captured as stale compile-time state.
- Multi-chip: instantiate device coordinates, route selection, link counts, shard ownership, and
  collective axes for one passing and one failing case.
- Precision: trace input, accumulator, intermediate, and output dtypes through the lowered C++ and
  kernel path rather than inferring them from Python alone.

## Guardrails

- Verify the diagnosis against current source before editing; reports are evidence, not authority.
- Do not require unrelated plugins. AutoDebug, AutoTriage, and AutoFix are self-contained in
  `tt-autodebug`.
- Do not reset devices, terminate other users' processes, push, or alter remote state unless the
  current task authorizes it.
- Preserve the user's unrelated working-tree changes.
