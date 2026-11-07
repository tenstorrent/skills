---
"tt-project": patch
---

A test gate rerun that selects failing tests by full node id works under pytest 9: the select plugin
computes the id relative to the current directory itself instead of calling
`config.cwd_relative_nodeid`, which takes a node id object in pytest 9 and crashed collection.
