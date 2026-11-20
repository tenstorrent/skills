---
"tt-project": patch
---

A lock changing hands raises no lock-wait alert or coordinator item: a few seconds with no holder while waiters queue is a hand-over, the old holder's item is settled and the wait counts again from the new holder. A lock left with no holder past a two-minute grace while waiters queue is named as possibly stuck and goes to the coordinator, still off the chat. The coordinator's digest no longer lists quiet alerts (web app and status only) as sent to the user.
