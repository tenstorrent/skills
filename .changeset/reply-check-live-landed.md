---
"tt-project": minor
---

A coordinator reply, notify or ask goes out exactly as written. When it says 'live' or 'landed',
each commit it names is checked once (running release for 'live', the push branch's local ref
for 'landed', no fetch), and any that fail add one footer line at the end, e.g.
`Unverified: 26b5acd3a not in running 0.2.1550`; an uncheckable commit reads `not checked`.
Status sentences are no longer tagged inline: a `claims` item that did not hold or could not be
checked is counted in the same footer, and a false status sentence only raises `claim_false`.
The sentence-level live/landed rewriting and tagging on the send path is gone.
