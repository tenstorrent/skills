---
"tt-project": patch
---

`tt-project`: a push waiting behind another push's priority slot now waits at most `checks.push_priority_wait_s` (account-level, default 1800 s), then names the holder and takes an ordinary host-wide checks turn.
