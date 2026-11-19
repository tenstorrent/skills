---
"tt-project": patch
---

Wait notes no longer read another project's task ids as this project's own. A ref with a known project's name right before it (`<peer> task #<n>`, `<peer> #<n>`, `<peer>'s #<n>`, `<peer>#<n>`, `<peer> task #<n> (run <r>)`), or in a list such a name starts (`<peer> #<n>, #<m> and #<k>`), is skipped. Known names come from the project registry, the senders of notes this project received and its peer list; this project's own name and plain words such as `needs` never count. Facts an earlier daemon stored from such refs (`task #<n> is done ...`) leave the hand-off's refs_over and the wait note on the next tick and are not stored again.
