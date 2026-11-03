---
"tt-project": patch
---

`checks.starve_s` is documented in the guide and the push-checks reference, and is read as the env
`TTP_CHECKS_STARVE_S`, then the account setting, like `checks.priority_turns`: the checks queue is
shared host-wide, so every waiter uses the same bound.
