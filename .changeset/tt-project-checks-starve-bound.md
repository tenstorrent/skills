---
"tt-project": patch
---

No checks run waits without a bound: one that has waited `checks.starve_s` (default 1 h, about one full
suite run) goes before later priority and push arrivals for the next free slot, also while it waits
outside the queue for its test slots. Push checks run on their branch's reserved test lane alone while
another run waits for test slots, so back-to-back pushes no longer hold a project's whole test budget.
The checks status line shows how long the push priority slot's holder has held it and how long each
waiter has waited.
