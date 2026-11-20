---
"tt-project": patch
---

`tt-project`:

- a remote check run whose origin host or ssh link died no longer fills its host: the run script traps hangup, interrupt and TERM, stops its command and removes its `ttp-check.*` folder (dash runs no EXIT trap when a signal ends the shell); the spill probe counts only folders a process of the ssh user still works in, or ones made in the last few minutes, and removes the ssh user's own dead folders after 2 hours (another user's folders, and hosts without /proc or GNU find, keep the 12-hour age bound); a host's own pool count (`checkcap.pool_runs`) counts the same way. tests
