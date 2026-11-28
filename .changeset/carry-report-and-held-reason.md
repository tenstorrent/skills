---
"tt-project": patch
---

carry: `ttp carry <name> report [--days N]` is model-free and prints the minutes tasks waited on depends_on (chain mode) against the minutes carry tasks waited, and the median lead time and push-queue/landed/deploy wait of orders that reached done. The scheduler log counts a queued carry task's unfinished depends_on under a new `carry` held reason, and the coordinator prompt says a carry task owns its order to live.
