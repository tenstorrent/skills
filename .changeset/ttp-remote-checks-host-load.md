---
"tt-project": patch
---

Checks that run on a remote check host are judged on that host: the load guard, `ttp checks --slot-status` and the `checks_host_overloaded` alert read the check host's own CPU demand (one cached ssh read; an unreadable host never holds work), never this machine's load. A fixed `checks.test_project_max` now also bounds remote checks and the push priority slot.
