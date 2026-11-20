---
"tt-project": patch
---

`tt-project`: the coordinator session spend guard counts a new session's seed as the one-time cost it is: the first two new-session seeds in its window count only the share of a day the window covers, later ones whole, so a rollover loop still trips and a full day's limit is unchanged. A first trip just over the allowance pauses the session for 1 h, rising to `coordinator.session.pause_h` at twice the allowance or on a repeat trip within 7 days, so a small overrun no longer keeps the on-by-default session off for 6 h. Trip records and messages show the spend counted, the seeds spread and the pause.
