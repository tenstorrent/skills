---
"tt-project": patch
---

`tt-project`: a worker whose sandbox keeps the tt-project home read-only can now run `ttp machines change edit` and `ttp machines outage` (set or `--clear`). Like `change add` and `close`, the CLI checks the request first (unknown entry or machine, closed entry, bad expiry, an edit that changes nothing) and queues it in the project's state; the project's daemon applies it once on its next tick. An edit keeps the entry's id, so a worker extends an entry before it goes overdue instead of closing it and adding a new one, which paged co-tenants. A request applied twice changes nothing more, and one refused at apply time is kept with its reason. A ledger write that still cannot happen (outside a project, or another file error) prints one clear line instead of a traceback.
