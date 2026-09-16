# Read order

Run the whole pass. Do not curate a subset — the output is one section per
script, cheap to grep, and skipping one just forces a re-run. Then read
top-down, narrowing as you go.

## The order

1. **What was running.** `dump_running_operations` first, for the op graph and
   the lowest active op id. `dump_op_mesh` next, for `[!]` stragglers.
2. **Integrity, out of order, as a trust gate.** `check_core_magic` and
   `check_binary_integrity`. Cheap, and they decide how much of steps 1 and 3
   you can believe. A corrupt mailbox invalidates every mailbox-reading script,
   which is most of the interesting ones.
3. **Why it is stuck.** `dump_callstacks` or `dump_aggregated_callstacks`, plus
   `dump_lightweight_asserts`. This is where the hang is actually diagnosed.
4. **Hardware, last.** Only once steps 1–3 have not pinned it on the workload.

## Why integrity jumps the queue

Steps 1 and 3 both read device mailboxes. If `check_core_magic` reports
corruption, their output is unreliable and the parked callstack you were about to
trust is fiction. When that happens, stop reading stacks and start looking for a
kernel doing an out-of-bounds NoC write — anchor on address arithmetic, not on
where cores appear to be waiting.

## Default to a software cause

A device rarely dies on its own. Treat a dead or degraded device as a symptom and
trace the software or configuration that drove it there. Conclude a genuine
hardware fault only when the evidence forces it and no software trigger is found.

## Looks alarming, usually is not

Do not spend the investigation on these:

- **Cores broken during triage** — triage halts cores to inspect them and may not
  resume them all. Matters only if essentially every core is reported.
- **NoC "mismatched state"** — a diagnostic observation, common across hangs, not
  a root cause alone.
- **"PC was not in range of any provided ELF files"** — read the *next* line. A
  continuation such as "Program cache is disabled" usually explains it benignly.
  Worry only when there is no continuation.
- **"Core is in reset"** — keep it in mind, do not over-invest.

## Missing output

**No sections at all.** Triage could not attach to a live process.

- Single process: the Metal process is dead. Triage needs it alive. Re-run with
  the process up.
- Multi-rank, "Inspector not found" on one rank: that rank's process finished or
  died. Expected for completed ranks — only still-hung ranks matter.

**Sections present but skipped or empty.** A data provider failed, commonly
Inspector, and its dependents were skipped. One upstream failure can blank most
of the report. Find the first non-skipped provider error, fix that root, re-read.
