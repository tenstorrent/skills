---
"tt-project": patch
---

`tt-project`:

- Terminal-Bench (Harbor terminus-2 on the chat endpoint) ranks served models first and decides their verdict; resumable per-(model, task) jobs, 429 waits, host trial cap
- Terminal-Bench runs the full task set of a local dataset folder, the 20-task subset first; a tasks file is an optional override (user msg #2801)
