---
"tt-project": patch
---

A spill host's quarantine really ends after 7 days at the latest: its machine ledger entry is now due at its expiry even while its known-good probe still says "not yet", so the auto-undo (`ttp checks --spill-back`) puts the host back instead of the entry paging as overdue. Other ledger entries keep the rule that a failing until-probe holds them past their expiry.
