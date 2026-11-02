---
"tt-project": patch
---

`tt-project`: a new `providers.<name>.billing` key (`auto` by default, `plan` or `usage`) declares how a provider is billed. `plan` with no plan windows means no dollar cap (the runaway guard counts runs) and its spend shows as plan, not billed; `usage` keeps the dollar caps on. A change raises the single budget-kind event and releases stale holds; `auto` behaves as before. Cursor's account label is now a hash of the login its CLI status names (`cursor:<hash>`), else `cursor`; the first upgrade shows one account-switched budget event for Cursor.
