---
"tt-project": patch
---

A coordinator turn that returns a refused `escalate` with only `noop` actions now counts as deciding nothing: its batch goes to the next turn once, as for a refused `escalate` returned alone.
