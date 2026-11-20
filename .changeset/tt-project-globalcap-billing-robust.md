---
"tt-project": patch
---

`tt-project`: global dollar cap. A pushed or pulled spend answer whose `other_sessions.billing` word this version does not know (a newer tt-project's) is no longer rejected whole: its billed project rows still count and its sessions are shown as estimated, not billed, so a version mix never undercounts. A local spend source naming such a word is shown the same way instead of dropped. Billing detection no longer depends on the daemon's environment alone: an API key exported only in an interactive shell is seen through the Claude Code login's approved-key record (usage billing with no subscription login beside it, unknown beside one). The session logs carry no sign of how a call was paid, so they are not used; an unknown login stays estimated, not billed, as before.
