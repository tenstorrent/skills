---
"tt-project": patch
---

Retired push-branch tips: a per-project state list of tips the push branch was replaced from (a
squash, a rewrite). The harness adds the old tip when its own lease push replaces the push branch
non-fast-forward, and `ttp retire-tip <sha>` adds one by hand. `ttp landed`, the push audit and the
unlanded-review sweep share one check that counts a commit under the current or a retired tip as
landed.
