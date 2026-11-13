---
"tt-project": patch
---

`tt-project`: the likely-correction tagger no longer tags a routine report as a repeat. A message whose every line only states what happened and names a commit (a push acknowledgment such as `pushed <sha> to <branch> (fast-forward of <sha>; ...)`) asks for nothing, so a second one is not flagged `repeats #N`. A repeated request, an order naming a commit, and a report line that also asks something are still tagged.
