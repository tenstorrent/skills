---
"tt-project": patch
---

No-device guard: `xargs -a <file> pytest` (also `--arg-file`, `-I {}` and other xargs options that take a value) and `python -c` code that runs pytest (pytest.main, or pytest through subprocess or os.system) are now refused outside `ttp sandbox`; a `python -c` that runs no test stays allowed.
