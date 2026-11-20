---
"tt-project": patch
---

`tt-project`: queued machine-ledger requests from a worker whose tt-project home is read-only are now idempotent and safer. Running the same `ttp machines change edit --expires 6h` or `ttp machines outage <machine> ... --expires 2d` again before the daemon applies it queues nothing more (the request is named by the command's own arguments, not by the end time it computes), as the CLI says. A queued `ttp machines outage <machine> --clear` right after a queued set now finds and withdraws that set instead of saying there is no known outage. A queued clear never removes an outage that was set after the clear was made: the daemon leaves the newer one in place and logs it.
