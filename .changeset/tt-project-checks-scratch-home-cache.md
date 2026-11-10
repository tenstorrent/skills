---
"tt-project": patch
---

`tt-project`:

- checks: a check's throwaway HOME links the cache in force (`XDG_CACHE_HOME` where set, else the real `~/.cache`) in as its `.cache`, here and on check, spill and Slurm hosts, so pip, uv, pre-commit and plugin caches are reused and checks run as fast as with the real HOME. A cache that is or holds a credential store is never linked, and removing the run's folder only unlinks the link.
