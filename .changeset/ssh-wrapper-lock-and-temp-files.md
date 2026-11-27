---
"tt-project": patch
---

The ssh wrapper's mkdir lock (hosts without flock) holds its holder's pid and is taken over only once that pid is gone, or after 30 s with no pid file, so a slow start never loses it. A killed call removes its `.err` and `.fail.<pid>` temp files and any start of its own, and the socket folder cleanup drops those of a dead pid. The header comment now says a call with options after the host opens no `-fN` master under the lock.
