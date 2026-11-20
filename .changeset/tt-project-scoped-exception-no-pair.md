---
"tt-project": patch
---

`tt-project`: the charter conflict check no longer pairs a scoped exception ('Exception to "<clause>" ...') with a ban as its permitting side. Its scope was checked when it was added, so an exception that shares wording with a second ban is not reported as conflicting with it. A plain permitting item that contradicts a ban is still paired.
