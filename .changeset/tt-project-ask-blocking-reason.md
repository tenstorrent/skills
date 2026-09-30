---
"tt-project": patch
---

`tt-project`: questions to the user need a real reason and never fall back on a timer.

- `ask_user` must carry `blocking`: one of access, funds, spend, review, merge, irreversible,
  restriction or human. Asks without one, or marked reversible, are rejected with "decide it
  yourself"; the rejection shows in the coordinator's next digest.
- Judgment calls are decided by the coordinator, recorded as a decision and sent as a low-severity
  notice.
- Asks still carry a `recommendation`, shown to the user so they can answer in one word. It is
  never applied without their answer: new asks never default to it. Asks registered earlier with a default still drain
  after `coordinator.ask_timeout_h`.
