---
"tt-project": patch
---

`tt-project`: a checks run that has waited past `checks.starve_s` now goes ahead only of later arrivals at its own priority or a less urgent one, so urgent runs keep their place under a backlog. An aged routine run still starts within a bound: once `checks.priority_turns` N more urgent runs (3 when unset; 0 lets it go first) took a slot while it waited, it takes the next turn. Push checks and priority-0 runs keep their precedence, and with no run past the bound the order is unchanged.
