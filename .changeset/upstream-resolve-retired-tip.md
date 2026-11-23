---
"tt-project": patch
---

`ttp upstream --resolve --fixed-in --task` accepts a fix that landed on a push branch tip retired by a history squash, at a version whose first commit is at or after the squash or at a pre-squash version that carries it on the retired tip; pre-squash versions without it stay refused.
