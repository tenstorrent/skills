---
"tt-project": patch
---

A quarantined spill host comes back once a known-good run passes there, not after a fixed 6 hours: its machine ledger entry's until-probe, `ttp checks --spill-probe <alias>`, runs the push branch tip's spillable checks there detached and lifts the host when they pass (a busy-host refusal is retried at once, with no strike). A quarantine that ends by time raises its back event and resets the host's strikes, and a quarantine whose failure later shows as a project-wide spill config fault is lifted.
