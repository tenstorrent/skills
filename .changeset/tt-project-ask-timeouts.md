---
"tt-project": patch
---

`tt-project`: reversible questions no longer stall a project.

- The coordinator marks each question to the user `reversible` or not and states its
  recommendation.
- A reversible question left unanswered for 12 hours falls back to its recommendation. The user
  is told in the chat what was decided and that they can reverse it. The coordinator then acts on it.
- Irreversible questions, and questions without a recommendation, always wait for the user.
- Nothing times out while the project is at a spend cap or while a user message is still unread.
- `coordinator.ask_timeout_h` sets the timeout; 0 turns it off.
