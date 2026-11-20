---
"tt-project": patch
---

Landing mark files whose holder died before letting go no longer pile up in the host's check-slots landing folder. Taking a new landing mark first removes every mark file that no live process holds and that is older than a minute, each under its own lock. A dead holder's file was already never counted as a landing (its OS lock is gone, even when its pid now names a live process); now it is also cleaned up. A mark that is held is never touched.
