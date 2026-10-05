---
"tt-project": patch
---

New opt-in config key `delivery.backup_remote` (off by default): the daemon pushes each finished code task's branch to that git remote, fast-forward only. It never forces, never pushes to main, master, the push branch or the base_ref (a value naming one is refused), and skips a non-fast-forward with one observation. A hand-off that finds uncommitted changes to tracked paths in the project's main checkout records one observation naming them, once per set of paths; nothing is committed or changed.
