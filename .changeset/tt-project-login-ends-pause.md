---
"tt-project": patch
---

tt-project: a logged-out pause ends by itself as soon as a login changes the provider's credential files (checked with a file stat, no model call), instead of waiting out the 15-minute probe.
