---
"tt-project": patch
---

The device job guard now expands `~` in `netfs_prefixes`, so a prefix like `~/net` matches the paths under it instead of silently matching nothing. Its script lint also accepts handlers written `function name { ... }`, so a job that traps EXIT, TERM and INT with such a handler is no longer refused.
