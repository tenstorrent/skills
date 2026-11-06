---
"tt-project": patch
---

`tt-project`: a "Resume wait" observation that is still queued, not yet read by the coordinator, is checked again each minute against the current reading of the spend gate and the other holds. If what holds the task has changed, for example a daily cap was raised and the spend gate cleared, the text is rewritten to name the current hold. The observation is retired once the task resumes or leaves the queue, or once a pause, a logout or a network hold takes it, since those report themselves. If the task still waits once such a hold clears, the same observation is queued again, so a short network or login blip does not lose it. Before, the coordinator could be sent after a spend gate that had already cleared.
