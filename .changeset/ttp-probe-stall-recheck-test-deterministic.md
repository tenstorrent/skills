---
"tt-project": patch
---

The test that a probe stall's re-check never holds the blocker sweep no longer flakes on a loaded host: the probe writes its pid file whole (it could be read empty), the test waits for the killed probe to be reaped instead of polling once, and its time bounds sit well under the probe's own run time.
