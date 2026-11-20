---
"tt-project": patch
---

`tt-project`: `ttp land --root <temporary clone>` lands on a main/master push branch without `delivery.allow_protected_push_branch` (the clone has no git remote, so nothing is published); in the project's own repo the refusal now names that setting and says why instead of "nothing to land on".
