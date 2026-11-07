---
"tt-project": patch
---

`tt-project`: an urgent approval arriving while a push batch searches for the entry that broke its checks no longer throws the search away when its verdict is near: with the verdict expected within `delivery.push_yield_wait_s` (default 900 s, from the batch's mean check time), the search goes on and the urgent approval leads the next batch. A set of approvals is stopped for at most `delivery.push_yield_restarts` times (default 1), and the approvals a stopped search requeues carry that count. The daily audit names the time from approval to landing (median and p90).
