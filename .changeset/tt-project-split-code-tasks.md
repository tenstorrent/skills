---
"tt-project": patch
---

`tt-project`:

- split code tasks: a code task that splits (changes on its branch plus a `continue:` hop, or a hand-off or head commit marking it WIP or split) is labelled `split:<head>` and gets no review; its hop takes the branch over and starts at once, never waiting on its own chain's landing. `task_add` and `task_update` refuse a `landed:#N` wait on a task in their own continue chain while N has not landed. The chain's last hop gets one review naming the chain and every hop's follow-ups. A failed review's findings go to an open continue hop of that branch instead of a separate Fix task.
