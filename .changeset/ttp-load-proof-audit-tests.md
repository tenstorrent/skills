---
"tt-project": patch
---

tt-project: the restriction-audit tests no longer fail on a loaded box. They ran the audit with the daemon's
5 s wall-clock budget, so on a busy host the tool scan was cut short and the over-note test failed once
(run 226). Tests now run the audit with no wall-clock cut; the budget's own tests still set 0.5 s and 0 s.
A new test drives a clock that moves 3 s per read and checks the scan still finds its tool.
