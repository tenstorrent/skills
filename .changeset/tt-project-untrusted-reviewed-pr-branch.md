---
"tt-project": patch
---

`tt-project`:

- a task labelled `untrusted_input` may push the pr_branch the coordinator recorded for it (a spec `pr_branch:` line or a task_update's, never one named only in quoted text) at exactly a head an independent review of the task passed; every other ref stays refused. The coordinator may clear the label with task_update `untrusted_input: false` and a `text` naming who vouched for the source (logged as an `untrusted_cleared` event; the task keeps an `untrusted_cleared` label); without a why, or from a task run, the clear is refused. Peer notes still set the label; tests
