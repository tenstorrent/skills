---
"tt-project": patch
---

tt-project 0.2.1000: a landing push's checks go to the spill hosts first, as `ttp checks` does. With `delivery.checks_spill_first` on (the default) and `delivery.checks_spill_hosts` set, `ttp push`, `ttp push --own` and `ttp land` send their `delivery.push_checks` to a spill host before taking this machine's push priority slot, and hold no local slot while they run there. Cheap checks that must stay here (an agent CLI) run here beside them. The checks run here under the priority slot only when the setting is off, every spill host is full or unreachable, or a check is pinned to its own host. A spill host that fails mid-run (the link drops) reruns that check here under a fresh priority slot. A nested run (already holding a checks slot) never spills.
