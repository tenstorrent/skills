---
"tt-project": patch
---

`tt-project`: the daemon's tick loop no longer evicts a finish attempt's remembered git answers. When the answers the tick loop keeps passed their 32 MB cap, the trim dropped the oldest answers of any kind, including those a running finish attempt had read ahead for its next rounds; that round then missed them and ran extra rounds, or failed as a git error. The trim now drops only the tick loop's own answers; the 256 MB cap on all kept answers is unchanged.
