---
"tt-project": patch
---

`tt-project`: Prompts now carry whole memory entries only: restrictions, preferences and resources first
(kept even past the budget, with a one-time coordinator alert), then the newest decisions and facts that
fit. Stale entries can be retired to `memory/archive/` with the coordinator's `memory_forget` action,
`memory_add` with `supersedes`, or `ttp memory <name> --forget <entry>`; replayed turns stay idempotent.
The coordinator's digest flags memory over budget, and the daily review retires stale entries.
