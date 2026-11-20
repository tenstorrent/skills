---
"tt-project": patch
---

A push batch a reboot cut short now resumes from its finished steps: at the daemon's next tick its
members still approved go again at once, alone and in its order, so the merged tree is the same and
its checks and test files that already passed there are not run again. A withdrawn or cancelled
member stays out and never lands; newer approvals wait for the next batch. The daemon log line, the
new batch's marker (`resumes`) and its log say what was kept and left out.
