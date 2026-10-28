# Continue hops

A done task's only follow-up titled `continue: ...` is queued by the daemon at the parent's tier (caps under `chains`).
A hop that only runs checks, pushes or edits spec or PR text costs less at light: title it `mechanical: ...`, or give the follow-up `"tier": "light"` (lower only, never above the parent's).
Its spec is unchanged; a light hop that needs judgment is retried a tier up.

A code task that splits (its branch has changes and it proposes a `continue:` hop, or its hand-off or head commit marks the head WIP or split) gets no review of its own.
Its hop takes the branch over and starts at once: never give it a `landed:#<task>` wait on its own chain (refused), as that work lands only with the hop's.
The chain's last hop, the first that ends without a split, gets one review of the whole chain; a failed review's findings go to an open hop of that branch, unless that hop waits on a task of the chain landing (it can add nothing first): then a Fix review task takes them.

A partial hand-off's hop (`partial: N of M cells`) leaves out cells no hop could produce: those in the hand-off's `cells_unrunnable` (a dict cell -> why, or `cell: why` lines), those whose newest `tried` entry in the chain's last 24 h is `rejected`, and those another open task names on its own `cells:` line.
With none left there is no hop; the reasons go in the result's `cells_no_hop` and the coordinator's event.
