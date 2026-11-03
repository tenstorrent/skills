---
"tt-project": patch
---

`tt-project`: a premise check's question now requires confirming, with git ls-tree or git grep at the push branch, that each file, function or symbol the premise, spec or upstream note names exists. A missing target with only an adjacent or similarly named one present fails the premise, naming what was found instead, and an upstream note naming paths outside the plugin (the sending project's own files) fails with a recommendation to close the note won't-fix.
