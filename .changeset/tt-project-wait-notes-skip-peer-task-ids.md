---
"tt-project": patch
---

Wait notes no longer read another project's task ids as this project's own. A ref with a known project's name right before it (`<peer> task #8`, `<peer> #8`, `<peer>'s #8`, `<peer>#8`, `<peer> task #8 (run 9)`), or in a list such a name starts (`<peer> #8, #9 and #12`), is skipped. Known names come from the project registry, the senders of notes this project received and its peer list; this project's own name and plain words such as `needs` never count. Facts an earlier daemon stored from such refs (`task #8 is done ...`) leave the hand-off's refs_over and the wait note on the next tick and are not stored again.
