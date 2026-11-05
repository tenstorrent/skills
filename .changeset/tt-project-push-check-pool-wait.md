---
"tt-project": patch
---

`tt-project`: a push check's time limit (`delivery.push_checks_timeout_s`) no longer counts time the check or any of its shards spends waiting for a test-pool slot, recorded with the new `ttp pool-wait -- <cmd>`. Pool waits have their own bound (`delivery.push_pool_wait_s`, default 4 h, 0 no bound); a check past it fails with `waited N s for a test-pool slot`, and the push log gives each check's pool-wait seconds.
