---
"tt-project": patch
---

`tt-project`: an owed checks run (its task owns an open obligation) no longer waits behind urgent checks that owe nothing. Each host-wide checks slot's label records whether its holder is owed; past `checks.preempt_after_s` an owed waiter asks an urgent holder that owes nothing to step back (after every routine holder, latest granted first), never an owed holder or a push check. A freed reserved urgent slot goes to the owed waiter first, then the urgent waiter that has waited longest, instead of whichever looks first. New `ttp checks --preempt <run>` sends the same request by hand, logs who asked, and refuses push checks, owed runs, runs behind a lock and runs on a reserved urgent slot.
