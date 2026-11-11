---
"tt-project": patch
---

`tt-project`: checks that fail tests on a spill host (no host fault, timeout or env fault) run the failing tests once more on this machine only, whose result counts: they never go on from spill host to spill host. A host's own fault (a wrapper refusal, an unreachable host) still moves them to another free spill host. A workstation keeps its verdict after two failing spill hosts.
