---
name: tt-triage-review
description: Reviews changes to tt-triage, the tt-metal post-hang diagnostic tool under tools/triage, by the principles its maintainers apply — trustworthy output from a broken system, failing loudly, letting the framework own cross-cutting work, one source of truth for hardware facts, output written for the reader, explicit and necessary code, and observing without perturbing. Use when a diff touches tools/triage/**, tools/tt-triage.py or tools/tests/triage/**.
metadata:
  tier: process
  upstream:
    - repo: tenstorrent/tt-metal
      ref: f83f3da72a381c79111c7417bed7bc4ed11830f8
      path: tools/triage/tt-triage.md
---

# tt-triage review

Assumes `tt-review-core`. tt-triage inspects a system that has **already failed**. It runs
after a hang, on hardware that may be in an unknown state. Its output is pasted into issues and
read by people who cannot reproduce the problem. CI and other tools also consume it.

This skill states what tt-triage maintainers consistently care about, distilled from a year of
their reviews. It deliberately names no APIs, because those change. **Learn the current
conventions from the repository each time** (see below), then judge the change against the
principles. `references/reviewer-principles.md` gives the reasoning and generic examples for
each one.

## Before reviewing: learn the current conventions

1. Read the tool's developer documentation in the triage directory, then the framework entry
   points the change uses: how scripts are declared, how they depend on each other, how they
   iterate devices and cores, report findings, raise fatal errors and emit results.
2. Read one existing script with the same role as the changed one, and treat it as the
   neighbour to compare against.
3. Note the pinned versions of the debugger library and the device driver library. Check any
   claim about their behaviour against those versions, not their latest code.

A finding that says "the framework already provides X" must name where X is.

## The principles

1. **Trustworthy output from a broken system.** Wrong information is worse than none.
   Identify devices, cores and processors without ambiguity: several numbering schemes coexist,
   users select subsets, devices can vanish after a failure, and runs can span processes and
   hosts. Never assume "the first device" or a default.
2. **Fail loudly, not silently.** Let unexpected errors propagate, so that tests catch drift such
   as renamed symbols or changed layouts. No catch-all handlers, and no fallbacks for states that
   cannot happen. Distinguish three cases: stop the script, report a finding and continue, and
   note something that is not a failure. Report at the right granularity: once per device, not
   once per core. Keep the original error.
3. **Script authors write minimal code; the framework owns cross-cutting concerns.** These
   include iteration and failure isolation, argument parsing, output rendering, verbosity and
   colour. Reimplementing one of them in a script is a finding. If the framework lacks
   something, extend the framework. Ask what the next script author will copy from this change.
4. **One source of truth.** Get hardware facts (topology, counts, addresses, block and
   processor kinds) and runtime state from the debugger library or the runtime's own metadata.
   Do not hard-code or recompute them. Logic that belongs in a lower layer moves there; tracking
   that with an issue is acceptable. Do not duplicate logic that exists elsewhere. When a change
   alters a shared pattern, update every instance of it.
5. **Public abstractions, explicit architecture handling.** Use the library's public interfaces,
   not private internals. Express architecture differences through the library's abstractions.
   Handle the known architectures explicitly; do not assume a future one behaves like a current
   one.
6. **Observe, don't perturb.** Restore any state the tool changes (halts, writes) on every path,
   including errors. Know and report what triage itself changed.
7. **Output written for the reader.** The default output shows what a non-expert needs;
   expert detail goes behind higher verbosity. Scripts return structured results and rendering
   happens in one place. Do not repeat information the output already carries. Use readable
   names with no unexplained abbreviations. A missing value is explicitly "not available", and a
   field always has one type.
8. **Explicit, precise code.** A name says exactly what something is or checks, including which
   numbering scheme an ID uses. One type end to end, with no redundant conversions. No magic
   numbers: name each one and take it from its source. Comments explain why, especially for
   non-default choices and workarounds. Docstrings are user-facing help, so they must be specific
   and current, and must name an owner.
9. **Only necessary code.** No speculative branches for cases nobody has seen, no unused members
   or methods, and no abstraction built ahead of need. Watch for over-built, generated-looking
   code. Prefer the simplest correct form.
10. **The right place for each concern.** CI-specific behaviour lives in CI. Configuration is an
    explicit option, not a hidden environment variable. Helpers that operate on one object's
    state belong to that object.
11. **Efficient on large systems.** Read only what you need, reuse data already read, and
    iterate the known set instead of scanning everything. Avoid waiting idly when ordering the
    work can achieve the same thing.
12. **Tests prove behaviour.** Exercise new or changed behaviour through realistic runs that
    would catch drift. Trivial unit tests of helpers add maintenance, not confidence.
13. **Tool and runtime ship together.** No backward-compatibility shims for older runtime
    metadata. Changes to shared data contracts update both the producer and the consumer.

## Applying them

- **Every finding needs concrete evidence** in the diff or the repository, plus the principle
  it serves, stated in a few words. If the principle's rationale does not hold for this code,
  it is not a finding. For example, polling inside a bounded measurement window is not idle
  waiting, and a guarded read of data that may be corrupt, where the corruption is the thing
  being diagnosed, is deliberate.
- Check claims about timing, ordering and control flow by tracing the code. Do not infer them
  from a rule. Before emitting a finding, re-read the callers and callees it depends on, and
  the behaviour before the change. A finding with a false premise is worse than no finding.
- Review what the change introduces or makes worse. Mention a pre-existing problem only
  when the change touches it, and say that it predates the change.
- Prefer fewer, high-confidence findings. In a long review, drop marginal `CONSIDER` items.
- If the better alternative does not exist yet, suggest tracking it. When intent is unclear, ask.

## Severity calibration

- Output that can identify the wrong device, core or value; device state left changed; a
  failure that can crash all of triage → `MUST-FIX`.
- Hidden failures, logic reimplemented or duplicated, missing tests for changed behaviour, and
  output that misleads → `SHOULD-FIX`.
- Naming, types, magic numbers, docstrings, verbosity placement, and unneeded code that is
  harmless → `CONSIDER`, unless it misleads a reader or user.
- Raise a severity above `CONSIDER` only for a concrete consequence you can show: a wrong
  value, lost state or a crash for `MUST-FIX`; a real maintenance or diagnosis cost for
  `SHOULD-FIX`. A principle alone does not raise it, and neither does documented, warned
  behaviour.

## Alongside other skills

- **`tt-comment-hygiene-review`** often runs too. It owns comment text, iteration-journey
  comments and explaining magic values. Raise a magic number here only when the value should
  come from its source of truth (principle 4). Emit each issue once.
- **Test apps that hang on purpose** (under the triage tests) exist to give triage something to
  diagnose. Review whether the failure is deterministic and documented, never "fix" the hang.
- Test expectations for triage come from principle 12, not from op-level coverage bars.
- A change to the runtime's debug-metadata producer also changes this tool's contract.

## References

| File | Read when |
|---|---|
| `references/reviewer-principles.md` | You need the reasoning behind a principle, or a generic example |
