---
"tt-project": patch
---

The bridge relay caps its carried connections (64 per listener, and a relay-wide cap sized to its open-file limit) and closes new ones at once over the cap; each upstream connect, name lookup included, gets one 15 s deadline across all addresses, so a sleeping workstation or a dropped VPN can no longer use up its open files.
