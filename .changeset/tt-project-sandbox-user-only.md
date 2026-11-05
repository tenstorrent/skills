---
"tt-project": patch
---

`tt-project`: the worker sandbox (`providers.<name>.write_fence`) is a user-only setting, as spend caps are: `ttp config` refuses to change it inside a run (directly or through a parent key), and a coordinator `config_set` changes it only in a turn carrying the user's message. The coordinator prompt and the harness guide say a user's harness-wide order (such as unsandboxed runs) applies to every project, and a project never opts out on its own judgement but records the workaround it relied on.
