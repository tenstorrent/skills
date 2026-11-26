---
"tt-project": patch
---

`tt-project`: the GLM guard no longer bounces a run to Opus for reading its guard files. A `cd` before plain readers (`cd <run>; head glm_guard.json`), `jq`, `ls`, and an inline `python3 -c "..."` read that the run-time Python judge watches now pass. Every edit of those files is still refused: a redirect, `tee`, `sed`, `cp`, `mv`, `rm`, `truncate`, `ln`, `xargs`, a computed name (`F=glm_guard.on; rm $F`), and a python write, which is refused up front with no run-time judge or as it runs with one.
