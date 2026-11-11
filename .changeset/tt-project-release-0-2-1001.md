---
"tt-project": patch
---

tt-project 0.2.1001: plan-billed providers (Codex on a prepaid plan, Claude on a Max plan) are reported by their plan windows, not their list-price dollars. `ttp stats --landing` counts only billed dollars per landed change; a plan provider's dollars are shown apart as virtual, with the points its 5-hour and weekly windows rose over the period, also per landed change. The daily self-efficiency audit gets the same plan-billed line, and the status line shows every other plan provider's window share ("codex plan 5h 5%, 7d 52%"), also next to a usage-billed account's dollars. Usage-billed spend (an Enterprise login or API key) stays in dollars. Virtual dollars still hold no work under a dollar cap; a test now covers a Codex plan.
