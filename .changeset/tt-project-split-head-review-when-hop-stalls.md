---
"tt-project": patch
---

`tt-project`: a split task's delivered head is reviewed now when its `continue:` hop stalls (waits on the user, a start_when, a block or a deferral past an hour), instead of waiting on the hop; the hop's later delivery gets a review of only its new commits. A done hop with no commits past its split head is still reviewed by the unreviewed sweep unless that head already has a review of its own, so a hop whose finish-time review was never queued does not leave the chain unreviewed.
