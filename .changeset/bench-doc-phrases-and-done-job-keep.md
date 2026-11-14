---
"tt-project": patch
---

`tt-project`: the model bench's fair filter also drops a task whose held-out test asserts exact wording of a doc, prompt or template the change wrote (whitespace-collapsed, so a phrase wrapped across lines counts) when the spec does not state it and the base lacks it; short or one-word strings still never count. A `done` hand-off no longer silently loses a live detached job: the Stop hook tells the worker once which of its live jobs the daemon will stop and how to keep (name it in retry_when or next_step) or stop each, and the daemon keeps a job the hand-off's message, summary or followups mention, raising one coordinator event naming the task, the job's .rc and the text that mentioned it. A job the hand-off never mentions is still stopped. The worker prompt says so.
