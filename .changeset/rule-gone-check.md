---
"tt-project": patch
---

`ttp rule-gone --msg <id> | --old "<text or value>" [--name CONST]` re-runs the older-rule sources (memory, charter, config, code constants and env defaults at the push branch tip) for a value in any unit. It exits 0 and marks the message's rule gone when none remains, else exits 1 listing each place. The rule-retirement digest line closes once gone and reads `overdue: user msg <id> rule still at <places>` after 24 h; the daily unblocking lines carry `Rules retired today: N (median message-to-gone H h); overdue: M`.
