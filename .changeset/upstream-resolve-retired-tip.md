---
"tt-project": patch
---

`ttp upstream --resolve --fixed-in --task` accepts a fix that landed on a push branch tip retired by a history squash, at a version cut after the squash (not on the retired tip and later than its version) or at a version whose first commit on the retired tip carries it; versions without it stay refused, even one a squash dropped from the old history.
