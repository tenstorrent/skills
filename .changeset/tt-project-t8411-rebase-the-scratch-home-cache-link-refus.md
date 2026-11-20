---
"tt-project": patch
---

`tt-project`:

- a spilled check's scratch HOME never links a cache that is or holds that host's HOME or a credential store (shell guard built from credguard's store list); a relative XDG_CACHE_HOME is never linked
- scratch-HOME rebase fix: the spilled-scratch-HOME test expects the host's shared checks cache for a .cache that links to HOME, as on the tip
