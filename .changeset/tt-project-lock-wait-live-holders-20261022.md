---
"tt-project": patch
---

Lock wait and busy messages (an exclusive task's wait, `ttp push`'s turn, a push batch, `ttp upgrade`)
name only live holders: a free slot's old label is no longer shown as its holder. Each holder now
shows its pid and whether that pid still lives (a keeper of detached jobs names its own pid), or the
bare pid where it cannot be judged from here.
