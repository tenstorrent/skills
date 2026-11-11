---
"tt-project": patch
---

tt-project: a check host whose tree copy timed out ("copying the tree timed out after 540 s") is now recorded as a staging fault, and `ttp checks --env-ok` on it passes only once a 1 MiB test copy to that host finishes within 30 s, not as soon as ssh answers, so a waiting task no longer wakes into the same timed-out upload; other check-host faults still clear once ssh answers
