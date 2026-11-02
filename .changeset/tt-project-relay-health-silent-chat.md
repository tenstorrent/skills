---
"tt-project": patch
---

Relay health reports real delivery gaps only: the web app's visible tab now counts as a reader (each poll records the newest message it showed), normal alerts count alongside asks, and a relay chat with no `ttp listen` running that left a message unread longer than relay.gone_after_s (default 24 h) stops counting as a reader and gets one low feed line until its next read. The status and web line names the silent chat and its last read and no longer asks whether the listener runs; the web shows it only while no ask is on screen.
