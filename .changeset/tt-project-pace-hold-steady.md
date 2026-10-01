---
"tt-project": patch
---

`tt-project`: plan pacing is steadier. A pace hold, once set after a run ends, can only move
earlier: it no longer drifts later on every idle tick until the two-hour cap. Burn on long windows
is measured over up to 12 h instead of 3 h, and a window over pace stays over until its burn falls
below 85% of the pace, so whole-percent readings no longer flip the gate between green and yellow.
The pace reason now says when the burn came from other sessions on the account, not this project.
