---
name: llk-race-audit-review
description: Reviews tt-llk kernel changes for the nine intra-kernel race hazard classes plus stale hardware config across invocations — cfg-word overlap, dataflow CB sync, instruction latency, mailbox sync, MMIO ordering, NoC sync, reconfig stalls, semaphore handshakes, and srcreg bank sync — plus the cross-class seams between them. Use when reviewing changes under tt_metal/tt-llk.
metadata:
  tier: kernel
  upstream:
    - repo: tenstorrent/tt-metal
      ref: ce91f33c0c7184618d60553e4b32910c5ebdbfaa
      path: tt_metal/tt-llk/.claude/skills/race-audit-all
    - repo: tenstorrent/tt-metal
      ref: fb5c6cfa6f08436079d10b0e2f794f6749c0ad42
      branch: bug-checker/program-cache-staleness-rules
      path: .github/bug_checker/rules/llk-stale-hw-config-state.md
---

# LLK race audit review

Assumes `tt-review-core`. Nine hazard classes plus the seams between them. Everything here is a
race: it passes intermittently, so "the tests pass" is not evidence. Nine are intra-kernel; a tenth
is cross-invocation and is where "correct in isolation" stops meaning correct.

## The nine intra-kernel classes

| Class | Hazard |
|---|---|
| `cfg-word-overlap` | Two writers touching the same config word, or a read-modify-write racing a concurrent update |
| `dataflow-cb-sync` | Producer/consumer CB handshake incomplete or reordered |
| `instruction-latency` | A dependent instruction issued before its producer's result is architecturally available |
| `mailbox-sync` | Mailbox write/read without the ordering that makes the value observable |
| `mmio-race` | RISC-to-Tensix MMIO ordering — writes not ordered against the consumer that depends on them |
| `noc-sync` | NoC transfer not barriered before its result is used |
| `reconfig-stall` | Reconfiguration issued while the affected unit is still busy |
| `semaphore-handshake` | Signal/wait imbalance, missing reset, or a wait on a stale value |
| `srcreg-bank-sync` | Source register bank swapped while a consumer still reads the previous bank |

## The tenth class — across invocations

The nine above are intra-kernel. A tenth hazard is **cross-invocation**: hardware config state is
never reset between kernel calls, so an op whose `_init_` / `_reconfig_` / `_uninit_` path does not
fully re-establish what it depends on inherits whatever the *previous* op left behind.

Read `references/cross-invocation-state.md`. It is separated from the nine because it defeats their
usual evidence: the op is correct in isolation and in its own unit test, and wrong only when
preceded by something else in a fused sequence or on a cache-warmed second call.

## Method

1. **Run each class against the diff separately.** Do not merge as you go — a class you have
   partially checked produces a verdict you will over-trust.
2. **Record the assumption behind every SAFE verdict.** The "safe because …" clause is the input to
   the next step; a SAFE with no stated assumption is unusable.
3. **Cross-reference the seams.** Any verdict whose safety is conditional on another class's
   invariant must be discharged against that class, at that site.

## The monotonic contract

This is what makes a combined sweep a genuine superset rather than a lossy concatenation. It is
non-negotiable.

- **Preserve every finding verbatim.** No dropping, merging away, or rewording.
- **The join may only ADD findings or ESCALATE severity.** Never silently delete or downgrade.
- **No silent downgrades.** If a cross-reference suggests a flagged item is actually safe, *attach an
  annotation* beside the original flag with the evidence. Do not replace the flag. Default to
  keeping it, and never promote "probably safe" to SAFE without proof at that exact site.
- **No summarisation at the fan-out boundary.** Reason over full finding lists, not digests.
- **No cap without a closer.** If coverage is bounded anywhere — sampling, a file left unopened, a
  verdict resting on an unconfirmed claim — state it *and treat it as a work item*. "I checked the
  main path and deferred the experimental files" is an **incompleteness that blocks the verdict**,
  not an acceptable caveat.

That last rule is the one that gets violated in practice, and it is the reason a bounded sweep must
never read as exhaustive.

## Ground or abstain

Every hardware claim must be grounded in a source available to the review. When it cannot be,
**abstain**: state the unresolved question, downgrade one severity step, and list it under
`Unresolved`. Do not infer hardware behaviour from surrounding code.

## Architecture scope

All nine classes apply on Wormhole, Blackhole and Quasar — but **verdicts do not carry across
architectures**, and two seams resolve differently on Quasar.

Read `references/architecture-scope.md` before auditing a Quasar-targeted change. The short version:
the MMIO-ordering seam resolves differently there, `instruction-latency` is architecture-divergent,
and the recall tool **pre-clears** some Quasar writes out of its findings while the underlying
tracking has exceptions — so a Quasar MMIO seam must be discharged against the actual consumer,
never against the tool's silence.

That last one is a false-negative generator. Nothing appears in the report at all, which is why it
needs stating rather than leaving to the tool.

## Severity

Races are `MUST-FIX` when the hazard is demonstrated at a site, `SHOULD-FIX` when the hazard class
applies and the discharging invariant is unstated, `CONSIDER` when the site is latent — reachable
only under a configuration the change does not currently produce. Latent is not safe; it is safe
*for now*, and it should say so.
