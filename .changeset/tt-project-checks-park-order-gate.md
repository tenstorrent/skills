---
"tt-project": patch
---

`tt-project`: an urgent or priority-1 worker may park or cancel another task's checks run only when one paragraph of its spec names the action itself (`ttp checks park|cancel`, or park or cancel before checks or queued runs) together with a user msg # or the coordinator; daemon notes such as 'the coordinator is cancelling #N' or 'parked' and 'cancelled' in other prose no longer count. `ttp checks queue` orders and ages its rows by each run's first arrival, not by its latest wait, which a re-wait resets.
