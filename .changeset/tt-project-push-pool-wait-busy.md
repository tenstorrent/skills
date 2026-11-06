---
"tt-project": patch
---

`tt-project`: a push check's time limit (`delivery.push_checks_timeout_s`) now counts run time: waits for a test-pool slot are left out, as waits for a grant already were, so a check that waited for a full pool and then ran inside its limit passes instead of failing. A check still waiting for a test-pool slot once its wall time (less grant waits) reaches the limit is ended as `busy, not failed` and `ttp push` exits 75 with a `retry_when`, never 4; a check that really ran past its limit still fails. `ttp pool-admit` leaves pool waits out of the time left the same way.
