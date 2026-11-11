---
"tt-project": patch
---

tt-project: cheaper and steadier runs. Without a chat provider, Claude Haiku at low effort can pre-screen event-only
coordinator wakes (opt-in, off by default: `coordinator.wake_triage_fallback` true; booked as `haiku:wake_decision`);
user messages, orders, must-act items and unblock events never wait for it. Standard-tier work, question and harness
tasks, and reviews up to `route.review_medium_lines` (400) lines, form a new `routine` class that can run on Sonnet
at high effort (opt-in, off by default: `route.core_cheap.routine` true); the coordinator, code, plans, device, deep,
urgent, pinned, review-fix and tier-up retry runs stay on their tier's model. Once on, an alarm fires when routine
tasks fail or need review more than 10 points more often than Opus tasks over 24 h. A Codex worker forked from the
coordinator's session gets a prompt that overrides the coordinator's no-tools rules. The run mark guard refuses only
a write, move or delete of the mark file. A worker's obligation receipt delivers only its own part, and
`ttp obligation reopen` undoes a receipt filed by mistake. An automatic review is not queued again for a head whose
review was cancelled by hand after the change's last run.
