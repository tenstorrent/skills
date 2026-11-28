---
"tt-project": patch
---

Hold escalation: ordered work blocked, waiting or deferred over `coordinator.hold_escalate_min` (default 5, 0 off) by a rule that is no safety gate raises one `hold_escalate` event naming the rule and the minutes, and proceeds `coordinator.hold_proceed_min` (default 5, 0 escalate only) later unless a safety gate is cited. A safety hold is never cleared and is raised once per hold episode (a held resource or a user pause is shown without a wake). Holds on an open task, an ask or review are left alone.
