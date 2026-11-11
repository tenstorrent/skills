---
"tt-project": patch
---

tt-project: cheaper and steadier runs. Without a chat provider, Claude Haiku at low effort pre-screens event-only
coordinator wakes (`coordinator.wake_triage_fallback`, on by default; booked as `haiku:wake_decision`); unblock
events never wait for it. Standard-tier work, question and harness tasks, and reviews up to
`route.review_medium_lines` (400) lines, run in a new `routine` class on Sonnet at high effort; code, plans, device,
deep, urgent, pinned, review-fix and tier-up retry runs stay on Opus. `route.classes.routine` `core` turns it off,
and an alarm fires when routine tasks fail or need review more than 10 points more often than Opus tasks over
24 h. A Codex worker forked from the coordinator's session gets a prompt that overrides the coordinator's
no-tools rules. The run mark guard refuses only a write, move or delete of the mark file. A worker's obligation
receipt delivers only its own part, and `ttp obligation reopen` undoes a receipt filed by mistake.
