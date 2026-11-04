---
"tt-project": patch
---

`tt-project`: prompts, the charter template, skills and docs no longer tell agents to wait, ask or skip a reset, power cycle, run or install because of neighbouring hosts, a `serving` tag or a site rule the harness infers. Only a hold or allowlist the user set (with the user's message as its source), a lock or device hold, a pause, a spend cap, a charter Restriction or a live tenant on that same box stops one; the setup-box `recovery.md` lists them, `box.py bare check` says a `serving` tag stops nothing, the ask rules ask only for concrete harm, and lulls are no longer asked for box resets or power cycles. A shipped-text test rejects the old wording.
