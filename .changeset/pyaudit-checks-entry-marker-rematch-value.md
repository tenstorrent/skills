---
"tt-project": patch
---

A run's inline Python can no longer write the check-slot upload locks or spill-down marks by importing the ttp package: the judge allows them only in the harness's own `python -c` checks entry. A re-matched older rule record now takes the current matcher's item, so a stale stored value is not kept. Records an earlier matcher version already re-matched are re-matched again. A config key or code constant that shares one word with the message now matches on a duration only when that word is within a few words of it, and a capital M ('100M') is a size, not minutes.
