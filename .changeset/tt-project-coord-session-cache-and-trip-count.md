---
"tt-project": patch
---

`tt-project`: a resumed coordinator session wake now writes its cache at 1h when a next wake is likely, unless the session's last 5m write is still warm (then 5m, as before). Before, a resume after gone 5m wakes went at 5m whenever the session's older 1h write was warm, but the cache lookback does not reach a 1h write behind several 5m wakes: those wakes read only the system prompt and re-wrote the whole history (about $0.8 at 150k tokens), then went at 5m again. A quiet project still goes at 5m. The session spend guard withdraws a trip on the same counted figure it tripped on: the first new-session seeds are spread over the day, and the window's spend and seeds stop at the trip time, so a new session started after the trip does not count against it.
