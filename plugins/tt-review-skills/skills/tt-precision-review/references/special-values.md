# NaN, Inf and denormals on Tensix

**Tensix is not fully IEEE compliant for special values, and the FPU and SFPU do not agree with
each other.** A rewrite, a fused path, or an engine change can therefore alter special-value
behaviour without touching the arithmetic. Source: tt-metal `tech_reports/Handling_Special_Value`.

## The two tables differ — that is the finding

FPU ops (`add_tiles`, `mul_tiles`, `matmul_tiles`):

```
 ±Inf × ±Inf   ->  ±Inf        Inf + Inf   ->  +Inf
 ±finite × ±Inf ->  ±Inf       -Inf - Inf  ->  -Inf
```

SFPU ops — the same four, **plus two the FPU table does not cover**:

```
 0 × Inf      ->  NaN
 +Inf - Inf   ->  NaN
```

So `0 × Inf` and `+Inf − Inf` have defined NaN results on the SFPU and are not specified for the
FPU. **A change that moves an expression between engines can change special-value output.** That
is a real review question on any fusion, decomposition, or SFPU-for-FPU substitution.

## Everything outside those tables is unspecified

Operations not listed treat NaN and Inf **as ordinary numbers**. The result may or may not be the
IEEE-designated special value.

Consequence for review: **do not accept "IEEE says so" as an argument** about a TT kernel's
special-value behaviour, in either direction. If a finding or a defence rests on standard
propagation for an op outside those tables, it rests on nothing.

Denormals are flushed — they read as `0x0` in every format.

## Detection is sticky, ORed, and output-only

Hardware sets status flags when a special value appears at the **output** of an FPU or SFPU op.
Flags are ORed across all lanes, so they say *something* saw it, never *which lane*. Bits cover FPU
underflow/denorm, FPU infinity/overflow, FPU int32 saturation, and SFPU NaN, infinity, denorm and
overflow separately.

Two properties matter for review:

- **The flags are sticky** — they persist until explicitly cleared. Code that reads them without a
  preceding clear may be reporting a *previous* op's special value. This is the same shape as the
  cross-invocation hazard in `llk-race-audit-review`: state that survives the thing that set it.
- **Detection is output-only.** Two blind spots follow, and they are worth naming in a finding
  rather than assuming coverage: special values arriving as *input operands*, and specials appearing
  as intermediates inside an FPU op that do not propagate to its output.

The read/clear helpers execute on the MATH thread only.

## What to flag

- A fusion, decomposition, or engine substitution with no statement about special-value behaviour,
  where the expression can produce `0 × Inf` or `Inf − Inf`.
- A special-value flag read with no clear before the region of interest.
- A claim of IEEE-standard behaviour for an op outside the two tables.
- Test coverage that never exercises Inf or NaN inputs on a path where they are reachable — a
  coverage finding, owned by `tt-test-coverage-review`.

## Connection to PCC triage

`SKILL.md` says a PCC collapse is a bug signal rather than a precision limit. Divergent
special-value propagation is one concrete mechanism: a single NaN or Inf entering a reduction
poisons the whole output and PCC falls off a cliff, which looks exactly like a precision failure and
is not one. **Check for special values before concluding a dtype is unusable.**
