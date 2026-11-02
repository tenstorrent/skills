---
"tt-project": patch
---

A git write inside a finish attempt drops that attempt's volatile and once git answers, so a read after it never replays an answer from before the write. `git reflog` and `git reflog show` count as volatile reads (capped, kept, never a strict round's point of no return); `reflog expire` and `reflog delete` stay writes.
