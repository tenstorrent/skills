---
"tt-project": patch
---

`tt-project`: a spec wake (an urgent task's spec change or a `wake_now`) whose run hands back the same wait (`waiting_for`, `retry_when`, branch head) now holds further spec wakes of that task on its `retry_when` for `waiting.spec_wake_hold_s` (new setting, default 7200 s; 0 turns it off) or until its own retry time, and its row says until when. Its probe, timer, a changed wait and every other wake still wake it; a `wake_now` during the hold waits for it to end unless the task is urgent and its wait key changed. Urgent spec wakes now share one wake within a 5-minute coalescing window instead of skipping it.
