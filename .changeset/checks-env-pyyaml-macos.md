---
"tt-project": patch
---

tt-project: the checks' pinned Python env has PyYAML on macOS too. macOS Pythons ship none, so the landing smoke's
yaml-importing test files failed collection whenever checks ran on the Mac (a spill falling back to it). The lock
now pins PyYAML (hash-checked) for `sys_platform == "darwin"` only; Linux hosts keep getting it from the OS
packages. An env's key leaves out lock lines for another platform, so the Linux hosts' existing envs are reused
as they are and a host with no package index never has to rebuild.
