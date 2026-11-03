---
"tt-project": patch
---

`tt-project`: `ttp say` without `--chat` now first prints the caller's unread replies in its reply chat (up to 20, oldest first) and marks read only the ones it printed, then sends the message. Before, replies stayed unread unless the caller ran the printed `ttp listen` line, so they counted as reaching no chat. A new `--wait <seconds>` option (at most 300) waits for the answer to the message just sent; it is forwarded to a project on another machine, dropped for an older runtime there, and left out of a queued message. A failure to read or mark the replies is reported on stderr and never stops or loses the message.
