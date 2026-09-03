---
name: ttnn-op-kernel-review
description: Structural correctness review for TTNN op kernels — init and data-format reconfig, TRISC synchronization, the tile_regs protocol, circular-buffer ownership and UB, work distribution, semaphores, control-flow CB balance, in-place misuse, op-level input validation, and program-cache correctness. Use when reviewing changes to reader/compute/writer kernels, program factories, or program descriptors under ttnn/ or tt_metal/.
metadata:
  tier: kernel
  upstream:
    - repo: tenstorrent/tt_ops_code_gen
      ref: e9c9417eee23c6783b5e72d6a2eed9f75f389fc4
      path: references/static-analysis-checklist.md
    - repo: tenstorrent/tt_ops_code_gen
      ref: e9c9417eee23c6783b5e72d6a2eed9f75f389fc4
      path: skills/debug-ttnn-op
    - repo: tenstorrent/tt-metal
      ref: ce91f33c0c7184618d60553e4b32910c5ebdbfaa
      path: .github/bug_checker/rules/reshape-dim-check.md
    - repo: tenstorrent/tt-metal
      ref: fb5c6cfa6f08436079d10b0e2f794f6749c0ad42
      branch: bug-checker/program-cache-staleness-rules
      path: .github/bug_checker/rules/program-cache-hash-collision.md
    - repo: tenstorrent/tt-metal
      ref: fb5c6cfa6f08436079d10b0e2f794f6749c0ad42
      branch: bug-checker/program-cache-staleness-rules
      path: .github/bug_checker/rules/smuggled-buffer-runtime-arg.md
    - repo: tenstorrent/tt-metal
      ref: fb5c6cfa6f08436079d10b0e2f794f6749c0ad42
      branch: bug-checker/program-cache-staleness-rules
      path: .github/bug_checker/rules/override-rebuild-in-cache-hit.md
    # PROVISIONAL: sourced from PR 54114, not yet merged. Re-pin to main on merge --
    # tt-skills-upstream-audit will flag this row once the branch goes away.
    - repo: tenstorrent/tt-metal
      ref: fb5c6cfa6f08436079d10b0e2f794f6749c0ad42
      branch: bug-checker/program-cache-staleness-rules
      path: .github/bug_checker/rules/op-shard-layout-validation.md
---

# TTNN op kernel structural review

Assumes `tt-review-core`. This skill finds **structural** bugs — the class that causes hangs,
silent numerical corruption, or undefined behaviour, and that only shows up when you reason about
data flow *across* kernel files. It is not a style pass and not a syntax pass.

## How to read a kernel change

Read reader, compute, and writer **as one unit**, together with the program descriptor and the
entry point. Build the model before you judge any line:

- Which thread produces each CB, and which consumes it?
- What order do operations run in, on which of the three TRISCs?
- What synchronisation sits between them?

A finding that does not rest on that model is a guess. Most of these bugs are invisible in a
single-file diff, which is exactly why they survive normal review.

## The ten categories

| # | Category | Read | Flag when |
|---|---|---|---|
| 1 | Init and data-format reconfig | `references/init-and-reconfig.md` | `compute_kernel_hw_startup` missing or wrong CBs; op-specific init missing; format reconfig skipped between differing CBs; `reduce_uninit` missing |
| 2 | TRISC synchronisation | `references/trisc-sync.md` | Two sequential ops share a CB with no push/wait path; a `NoWait` policy without a data guarantee |
| 3 | `tile_regs` protocol | `references/trisc-sync.md` | acquire/commit or wait/release unbalanced; DST expected to survive `release()` |
| 4 | CB ownership and UB | `references/cb-ownership.md` | More than one producer or consumer thread; tile count does not divide CB pages; inconsistent wait counts; push/pop outside the reserve/wait window |
| 5 | Work distribution | `references/work-distribution.md` | `group_2` can be zero-work; reader/compute/writer tile counts disagree; one runtime-arg count for both core groups; `CoreRange` off-by-one |
| 6 | Semaphores | `references/semaphores.md` | `noc_semaphore_inc` with no atomic barrier; no reset inside a gated loop; multicast destination count wrong; signal/wait totals unbalanced |
| 7 | Control-flow CB balance | `references/control-flow-and-inplace.md` | CB ops unbalanced across `if`/`else`; early return holding pages or DST; producer and consumer loop counts disagree |
| 8 | In-place misuse | `references/control-flow-and-inplace.md` | Output routed to a third CB and copied back; output CB aliases an input under a non-streaming lifecycle |
| 9 | Op-level input validation | `references/op-input-validation.md` | Reshape changes logical volume; layout alignment unchecked; `shard_spec().value()` without a sharded guard; a validator that never constrains a layout the factory assumes |
| 10a | Program cache hashing | `references/program-cache-hashing.md` | A custom hash omitting a field `create()` reads; optional inputs not hashed as present/absent; shape reduced to rank or volume |
| 10b | Runtime args on the cache-hit path | `references/program-cache-runtime-args.md` | A buffer address written to RTAs without registration; `create_descriptor()` reached from `override_runtime_arguments()`; an early return that skips address patching |

Categories 1–8 are correctness *inside* the kernels. **9 and 10 are host-side** — whether the op
checked what it was handed, and whether it survives being cached. Both are distilled from merged
fixes rather than guidance, and both are invisible in a single-invocation test: category 10
especially, where a wrong cache *hit* produces wrong results with no error and the first call always
passes.

## Severity calibration for this domain

Most findings here are `MUST-FIX`, because the failure modes are hangs and silent corruption rather
than inefficiency. Two qualifications:

- **Silent corruption outranks a hang.** A hang is loud and gets fixed. A missing init or a missing
  `reduce_uninit` produces wrong numbers with no error at all, and can ship.
- **"It passes today" is not evidence of correctness.** Race conditions and UB pass intermittently.
  Undefined behaviour that works for small tile counts fails non-deterministically at larger shapes.
  Say so in the finding rather than softening the severity.

## Helpers change who is responsible

`kernel_lib` helpers handle init, reconfig, and `tile_regs` internally **under default policies**.
Responsibility shifts to the caller when a non-default policy is used or when raw APIs are mixed in.

**Do not flag a helper call for a missing init that the helper performs itself.** Check the policy
first: default `DataFormatReconfig` is `INPUT_AND_OUTPUT`, and the reduce helper's `Accumulate`
handles spill/reload/re-init. This is the most common false positive in this domain — it makes the
reviewer look like it has not read the helper.

## Evidence for this domain

Cite the specific kernel file and line plus the counterpart: for a synchronisation finding, cite
*both* the push site and the absent wait site. For a CB UB finding, cite the descriptor line that
sets `num_pages` alongside the call using a non-dividing tile count. A structural finding with only
one end of the pair is not actionable.
