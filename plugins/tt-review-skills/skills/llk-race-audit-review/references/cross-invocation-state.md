# Tenth class — stale hardware config across kernel calls

The nine classes in `SKILL.md` are intra-kernel. This one is **cross-invocation**, and it is the
reason a kernel can be correct in its own unit test and wrong in production.

LLK kernels program persistent hardware configuration — unpacker tile descriptors and strides,
packer formats and L1 offsets, ALU format-spec and accumulate-control fields, ADDR_MOD slots, and
the software trackers mirroring them. **None of it is reset between invocations.** Whatever the
previous op left is what the next op starts with.

The bug appears when an op's `_init_` / `_reconfig_` / `_uninit_` path fails to re-establish the
state it depends on, so behaviour depends on **which op ran before it**. Symptom: correct in
isolation and in its own unit test, wrong only when preceded by another op in a fused sequence or on
a cache-warmed second call. No crash — subtly wrong datums, or a hang.

Roughly thirty merged fix PRs share this root cause. See `SOURCES.md`.

## What to look for

**1. Partial field update in a reconfig.** A `_reconfig_` / `_init_` that writes some fields of a
config register group but not others the same op depends on. Cross-check against the matching
`_llk_*_hw_configure_` for that unit: every field the full configure sets is a field a reconfig must
either set, or provably not care about.

Also **diff the architecture siblings** — a field handled on one architecture and missing on another
is a strong signal, and it is a cheap check.

**2. Unmasked full-word write to a shared config word.** `TTI_WRCFG(..., p_cfg::WRCFG_32b,
X_ADDR32)` or a raw `cfg[X_ADDR32] = ...` targeting a word holding more than one named field. This
zeroes every sibling field in the word — including fields owned by another thread or another
concern. The correct form is a masked read-modify-write (`cfg_reg_rmw_tensix<X_RMW>(value)`), which
is byte-atomic and touches only its own field.

This overlaps `cfg-word-overlap` from the nine, but the framing differs: there the hazard is two
concurrent writers, here it is one writer destroying a neighbour's *persisted* value.

**3. Direct config write bypassing a software state tracker.** Writing a tracked field directly
without invalidating the tracker afterwards. The tracker's configurators early-return when the
recorded state already matches, so **a stale tracker silently suppresses the next genuinely needed
re-apply.** The write appears to succeed; the suppression happens later, in a different op.

**4. Missing or mis-ordered `_uninit_`.** An `_init_` that installs an ADDR_MOD, MOP program, or
format override with no `_uninit_` restoring it — or an `_uninit_` whose calls run in an order
leaving the unit half-configured. Check the sanitizer's init/uninit pairing and that the API-order
contract it asserts is actually satisfied.

This is the shape that **breaks the next op rather than itself**, which is why it survives review:
the failing test names an op that is not the one at fault.

**A no-op teardown can be correct.** Where the state an `_init_` installs is transient and
reprogrammed by the next operation's init, there is genuinely nothing to restore, and the in-tree
implementations that do this say so in a doc comment. Read the comment before flagging an empty
`_uninit_` — "the uninit does not restore what the init set" is a question, not a verdict.

**5. Config rewrite with no execution-unit drain.** Config the hardware samples during an
in-flight op must not be reprogrammed while that unit is running. **Drain every unit that reads
the field being written — no more, no less.** This is architecture- and field-specific; see
`references/config-drain-stalls.md`.

## Reviewing this class

Order-dependence is the tell, and it defeats the usual evidence. "The test passes" means the op was
tested in isolation, which is precisely the condition under which this class is invisible. Ask what
ran before it.

Findings are `MUST-FIX` — silent numerical corruption or a hang. State the dependence explicitly:
name the predecessor op or sequence that exposes it, so the fix can be tested rather than reasoned
about.
