# Architecture scope and per-arch divergence

All nine classes apply on Wormhole and Blackhole, with the cross-references as described in
`SKILL.md`. Quasar diverges in ways that change how specific seams resolve — the classes still
apply, but two of them resolve differently and one tool behaviour will mislead you.

## Quasar: the MMIO-ordering seam

Hardware AutoTTSync changes the RISC-to-Tensix MMIO-ordering class. Wormhole and Blackhole need
manual ordering; Quasar does not, so **any seam touching `mmio-race` resolves differently** and a
verdict carried over from a WH/BH audit is not valid there.

**The trap, and it is the important part of this file.** The recall tool blanket-tags Quasar
cfg/GPR writes as `AUTOTTSYNC_ORDERED` — it **pre-clears them out of `findings[]`**. But TTSync's RQ
tracking *excepts* `MOP_CFG`, `REPLAY(load=1)`, `RESOURCEDECL`, and post-load-replay consumers.

So a Quasar MMIO seam **must be discharged against the actual consumer, never against the tool's
silence.** The tool not flagging something is not evidence that it is ordered — for those excepted
cases it is precisely where the tool is wrong. This is a false-negative generator, which is worse
than a false positive: nothing appears in the report at all.

## Quasar: instruction latency

`instruction-latency` is also architecture-divergent — Blackhole and Quasar scoreboard, Wormhole
always pads. A latency verdict is per-architecture and does not carry across.

## Seams that still apply everywhere

`cfg-word`, `semaphore`, `reconfig`, `mailbox`, `dataflow-cb`, `srcreg-bank`, `noc`.

Verify Quasar mailbox, NoC, and unpack-to-dest semantics before extending a verdict there. Note in
particular: **the CB API is architecture-agnostic, but its NoC ordering primitives are
architecture-specific.** A CB-level argument that holds everywhere can rest on an ordering primitive
that does not.

## Grounding

Each sub-audit carries its own Quasar caveat — honour them in the join rather than overriding them
from here.

Ground every hardware claim before relying on it. The sources differ by architecture and by claim
type: architecture documentation for Wormhole and Blackhole, the compiler for latency facts, and for
Quasar the ordering guarantees live in internal documentation that a public CI run cannot reach.

**Ground or abstain, and flag any fallback verdict as such.** Where the grounding is unreachable
from where the review is running, say so in `Unresolved` and downgrade — do not infer the guarantee
from surrounding code, and do not carry a Wormhole/Blackhole verdict across by analogy.
