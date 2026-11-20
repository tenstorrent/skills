---
"tt-project": patch
---

The push queue now also records an upstream note fixed when only the specs of the covered tasks and
their continues chain name it as `[note <fp>]`, so a split or fix task that drops `resolves` no longer
leaves the note open. Unknown and already-resolved fingerprints are ignored.
