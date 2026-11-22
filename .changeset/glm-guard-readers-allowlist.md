---
"tt-project": patch
---

GLM write guard: only readers with no output-file form and no command-running form (`cat`, `grep`, `egrep`, `fgrep`, `head`, `tail`, `wc`, `cut`) may name a run's guard files; `uniq IN OUT`, `sort -o`, `less -o` and `rg --pre` on them are refused.
