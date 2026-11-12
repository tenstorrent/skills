---
"tt-project": patch
---

The hands-on audit counts `ps -o <format>`, `pmset -g` and `scutil --nc list|status` as reads: ps's `-o` (and pgrep's, ss's, lsof's) is no longer taken for a written file. Other pmset and scutil forms and inline `python3 -c` stay changes.
