---
"tt-project": patch
---

`tt-project`: an urgent approval arriving while a push batch searches for the entry that broke its checks no longer stops the search by default: it joins the search's next split, the search finishes with no check thrown away, and the urgent approval goes first in the next batch, which starts as soon as the running one ends. Stops are opt-in with `delivery.push_yield_restarts` (default 0: never), and even then a verdict expected within `delivery.push_yield_wait_s` (900 s, from the batch's mean check time) is waited for. A stopped search hands what it learned to the requeued approvals: the next batch of exactly those entries takes the full head's failure as known and skips that check, rechecking it only if every smaller prefix passes. The daily audit names the time from approval to landing (median and p90).
