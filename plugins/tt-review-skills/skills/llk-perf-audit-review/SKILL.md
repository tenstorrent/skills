---
name: llk-perf-audit-review
description: Static performance review of Tensix compute kernels — wasted cycles, redundant register and memory traffic, loop-invariant work, and cheaper numerically-equivalent instruction sequences. Gated by a provenance lens (sfpi-compiled versus hand-written) and a semantic-equivalence test. Use when reviewing SFPU or Tensix kernel changes under tt_metal/tt-llk.
metadata:
  tier: kernel
  upstream:
    - repo: tenstorrent/tt-metal
      ref: ce91f33c0c7184618d60553e4b32910c5ebdbfaa
      path: tt_metal/tt-llk/.claude/skills/perf-optimization-audit
    - repo: tenstorrent/tt-metal
      ref: ce91f33c0c7184618d60553e4b32910c5ebdbfaa
      path: tech_reports/Handling_Special_Value/special_values.md
---

# LLK performance audit review

Assumes `tt-review-core`. Finds cycles wasted on bubbles, redundant traffic, work that should not
be in the loop, and sequences a cheaper equivalent computes identically.

**This is not a correctness audit.** Where a latency fact overlaps `llk-race-audit-review`, that
skill owns the correctness verdict — a missing NOP causing silent corruption is its finding. This
skill owns the perf verdict: a bubble that could hold useful work, or a NOP that is provably
redundant. Cross-link, never contradict.

## The provenance lens — apply this first

It decides which findings are even valid, and skipping it is how this domain generates noise.

**sfpi-compiled code** (`vFloat`, `dst_reg[...]`, `sfpi::` ops): the compiler already schedules
instructions, inserts and omits NOPs, allocates registers, and does basic scheduling. So:

- **Do not file "add a NOP" or "manually interleave to hide this stall."** That is the compiler's
  job; doing it by hand is noise at best and a pessimisation at worst.
- **Do file algorithmic findings** — fewer instructions, fewer Dst stores, hoisting, branchless
  rewrites, builtins, FMA, approx-mode gating. These are what the compiler cannot do, and where the
  real wins are.

**Hand-written `TTI_*` / inline-asm / raw opcode sequences** bypass the scheduler. Manual
interleaving to fill latency shadows — and removing NOPs made redundant by reordering — **is** valid
here. This is the only place the "hide the bubble" class lives.

Classify every block before applying any check. A finding filed against the wrong provenance is
wrong regardless of how good it looks.

## The semantic-equivalence gate

Every finding must be **numerically identical** to the original — bit-for-bit, or within the op's
stated tolerance under `APPROXIMATION_MODE`.

For each finding, **state the equivalence argument** and check the edge cases the rewrite could
perturb: NaN, positive and negative infinity, denormals, signed zero, rounding mode, and the exact
boundary values such as `x == 0` for a step or shrink op. A `max(x, 0)`-for-`v_if` rewrite has to
match the original's NaN and −0.0 behaviour, and frequently does not.

If equivalence cannot be established, the finding is a **suggestion requiring test confirmation** —
never a confident win.

**Know what the hardware actually does before asserting equivalence.** Tensix is not fully IEEE
compliant here, and the FPU and SFPU differ: `0 x Inf` and `Inf - Inf` are defined NaN on the SFPU
and unspecified on the FPU, and ops outside those tables treat specials as ordinary numbers. So a
rewrite that moves an expression between engines can change special-value output while looking
arithmetically identical. `tt-precision-review`'s `references/special-values.md` has the tables.

## False-positive guards

Scalar-CPU intuitions that do not transfer. Each of these is a finding a reviewer will want to file
and should not:

- **Branch reordering by likelihood buys nothing** under SIMD predication. All lanes evaluate every
  branch; there is no branch prediction and no short-circuiting. Do not file "put the common case
  first."
- **The `v_else` in an if/elseif/else costs no extra compare** — it reuses the complemented
  predicate. A three-region op genuinely needs two compares. That is the floor, not waste.
- **Do not hand-insert NOPs or manual scheduling into sfpi-compiled code** — see the provenance lens.
- **Do not replace common float literals with const registers.** The pinned compiler already lowers
  them, so the rewrite is a no-op.
- **Do not remove a drain or stall for performance** unless it is provably redundant *for ordering
  too*. That is a correctness call, and it belongs to `llk-race-audit-review`.

## Verdicts

| Verdict | Use when |
|---|---|
| `PERF-WIN` | Numerically equivalent, provably removes instructions, stores or bubbles. Give before → after and a magnitude class. |
| `SUGGESTION` | Plausible, but equivalence or magnitude is not proven statically. Recommend the perf-counter check. |
| `ALREADY-OPTIMAL` | At the instruction floor for its semantics. Say why; do not invent a win. |
| `NON-ISSUE (guarded)` | Matched a pattern, killed by a guard or the provenance lens. Record it so it is not re-raised. |
| `UNCERTAIN` | Cannot resolve the pinned compiler or throughput facts. Abstain and mark coverage bounded. |

**Never guess a latency or throughput number.** An invented cycle count is the most damaging thing
this skill can produce, because it is precise, plausible, and unfalsifiable without a rerun.

Prefer a magnitude class — large, moderate, marginal — over false precision. Estimate as
instructions or stores removed per element, times iterations, times tiles.

## Architecture scope

**Report per-architecture; never collapse to one verdict.** Wormhole, Blackhole and Quasar differ in
scheduling, in latency and throughput, and in available instructions — so a win on one may be a
no-op or a pessimisation on another. A fix to a byte-identical copy applies to all; a divergence
between the copies may itself be the finding.

**Grounding differs by architecture.** Latency and throughput facts come from architecture
documentation for Wormhole and Blackhole, and from the compiler for scheduling. Quasar timing is not
in the same source — it lives in its own microarchitecture documentation, reachable from a developer
machine but generally not from public CI.

Where you cannot reach the grounding for the architecture in question, return `UNCERTAIN` and mark
coverage bounded. That is the rule from the verdict table applied per-architecture, and it matters
most here: **a guessed latency number is the most damaging output this skill can produce**, because
it is precise, plausible, and unfalsifiable without a rerun.
