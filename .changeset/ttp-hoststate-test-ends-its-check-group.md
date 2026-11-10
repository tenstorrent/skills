---
"tt-project": patch
---

The host-guard tests run their check with a 2-minute timeout and kill its process group on the way out (pass, failure or timeout), so a hung stand-in or sudo never outlives a stopped test worker; a new case checks that a hung sudo read leaves nothing running.
