---
"tt-project": patch
---

Push queue fixes from review: the unreachable-remote probe asks with `--exit-code` through the
memoized git helper, so a remote that answers without the branch keeps the hold (one alert, one note
to the coordinator); a batch stopped by a changed `delivery.push_branch` keeps an entry `landed`
only where the new target has it.
