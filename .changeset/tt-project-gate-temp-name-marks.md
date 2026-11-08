---
"tt-project": patch
---

`tt-project`: the tests gate no longer treats one failure as "failed on the base too but another way" when the head and base runs differ only in the random part of a temp name in a path (`home-y7h9wyh0` vs `home-w56a_m92` from mkdtemp, `check.T6w5cg3S` from mktemp). Such failures are now judged pre-existing, so a spill host's check passes there instead of rerunning on the project host. Different messages and the numbers of an assert still count as a difference.
