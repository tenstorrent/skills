---
"tt-project": patch
---

`tt-project`: the `[facts at send time]` suffix on `ttp note --to` (and the worker prompt's facts block) attaches a task record only to a `#N` that names a task: `task #N` or a bare `#N`. A number led by another noun (ledger, entry, change, msg, ask, alert, note, issue, PR, hold and similar) is skipped, and every number of a run like `#1, #2 and #3` takes the noun leading it. A number led by an unknown word keeps its line, labelled `task #N (if meant)`.
