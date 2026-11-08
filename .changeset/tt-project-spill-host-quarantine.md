---
"tt-project": patch
---

A spill host whose spilled runs keep failing where the same commit passes elsewhere is quarantined per project after `checks.spill_quarantine_after` strikes in a row (default 3): `ttp checks` skips it, one event and one machine ledger entry record it, and the entry's auto-undo (`ttp checks --spill-back <alias>`) puts it back. A failure every spill host shares (a broken spill setup) raises one incident and quarantines no host.
