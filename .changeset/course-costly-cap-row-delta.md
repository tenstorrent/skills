---
"tt-project": patch
---

Course check: the 'costly:' list shows at most 10 tasks (open ones first, then the costliest finished), then one count line naming the rest and `ttp course <project>`, a new read-only command that lists them all. A session wake sends the Course check row by row, keyed by sign and task id, so a costly line already shown and unchanged is not resent. On a replay of 25 recorded session wakes of one large project, resumed prompts shrank 49% (about 50 KB of Course check per wake down to 0.3-3.3 KB).
